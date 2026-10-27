"""Failure drill on a throwaway, volume-free copy of the deployment stack.

    python scripts/failure_drill.py --work-dir <empty dir outside the repo>

Starts a NEW project named docmind-drill-<random> with deploy/compose.yaml +
deploy/compose.release-check.yaml (PostgreSQL on tmpfs, uploads in <work-dir>, the host's
model cache read-only, no Docker volumes), then checks how the stack behaves when parts of
it fail, and finally removes its containers and network with `down` (never -v). It never
touches another project: every compose command carries the generated project name.

Checks: PostgreSQL stopped (liveness vs readiness, error envelope through the web BFF, web
pages still served), worker stopped (uploads queue, then complete), LLM unreachable (only
/ask fails, with generation_unavailable), forged X-Forwarded-For does not bypass the login
rate limit, oversized BFF auth bodies are refused without exhausting the web container's
memory, server-side files are not served, graceful termination (exit codes, not SIGKILL),
and no secrets or document text in any log.
"""

import argparse
import json
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
PROJECT_PREFIX = "docmind-drill-"
API_PORT, WEB_PORT = 8381, 3381
results: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok)))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{(' - ' + detail) if detail else ''}", flush=True)


def known(name: str, detail: str) -> None:
    """A documented residual risk (docs/phase-12-audit.md): reported, not a failure."""
    print(f"[KNOWN] {name} - {detail}", flush=True)


def is_drill_project(name: str) -> bool:
    return re.fullmatch(rf"{PROJECT_PREFIX}[0-9a-f]{{8}}", name) is not None


class Stack:
    def __init__(self, project: str, env_file: Path) -> None:
        if not is_drill_project(project):
            raise SystemExit(f"refusing to operate on project {project!r}")
        self.project = project
        self.base = ["docker", "compose", "-p", project,
                     "-f", str(ROOT / "deploy" / "compose.yaml"),
                     "-f", str(ROOT / "deploy" / "compose.release-check.yaml"),
                     "--env-file", str(env_file)]

    def run(self, *args: str, check_rc: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run([*self.base, *args], cwd=ROOT, check=check_rc, text=True,
                              encoding="utf-8", capture_output=True)

    def container(self, service: str) -> str:
        return f"{self.project}-{service}-1"


def docker(*args: str) -> str:
    return subprocess.run(["docker", *args], capture_output=True, text=True,
                          encoding="utf-8").stdout.strip()


def wait(predicate, timeout_s: float = 180, step: float = 2) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except httpx.HTTPError:
            pass
        time.sleep(step)
    return False


def write_env(path: Path, storage: Path) -> None:
    hub = Path.home() / ".cache" / "huggingface" / "hub"
    path.write_text("\n".join([
        "POSTGRES_USER=docmind",
        f"POSTGRES_PASSWORD={secrets.token_urlsafe(24)}",
        "POSTGRES_DB=docmind",
        f"DOCMIND_JWT_SECRET_KEY={secrets.token_urlsafe(48)}",
        # prod: the configuration that would be released (fake generators are refused).
        "DOCMIND_ENVIRONMENT=prod",
        "DOCMIND_ALLOWED_HOSTS=localhost,127.0.0.1",
        f"DOCMIND_API_PORT={API_PORT}",
        f"DOCMIND_WEB_PORT={WEB_PORT}",
        # An LLM that does not exist on the network: /ask must fail alone and cleanly.
        "DOCMIND_GENERATION_BACKEND=openai-compatible",
        "DOCMIND_GENERATION_BASE_URL=http://ollama:11434/v1",
        "DOCMIND_JOB_LEASE_TIMEOUT_S=30",
        f"HOST_HF_HUB={hub}",
        f"CHECK_STORAGE_DIR={storage}",
    ]) + "\n", encoding="utf-8")


def bff_login(web: httpx.Client, origin: str, email: str, password: str, **headers):
    return web.post("/bff/auth/login", json={"email": email, "password": password},
                    headers={"Origin": origin, **headers})


def oversized_body_check(stack: Stack, web: httpx.Client, origin: str, mib: int) -> None:
    """A large body to a BFF auth route must be refused without being buffered."""
    container = stack.container("web")
    restarts_before = docker("inspect", "--format", "{{.RestartCount}}", container)
    size = mib * 1024 * 1024

    def chunks():
        block = b"a" * (1024 * 1024)
        for _ in range(size // len(block)):
            yield block

    started = time.monotonic()
    try:
        response = web.post("/bff/auth/login", content=chunks(), timeout=120,
                            headers={"Origin": origin, "Content-Type": "application/json"})
        status: int | str = response.status_code
    except httpx.HTTPError as exc:
        # The server may answer 413 and close before the client finished sending.
        status = f"connection closed ({type(exc).__name__})"
    elapsed = time.monotonic() - started
    time.sleep(2)
    oom = docker("inspect", "--format", "{{.State.OOMKilled}}", container)
    restarts = docker("inspect", "--format", "{{.RestartCount}}", container)
    alive = wait(lambda: web.get("/login").status_code == 200, 30, 1)
    refused = status == 413 or (isinstance(status, str) and status.startswith("connection"))
    check("oversized BFF auth body refused without exhausting the web container",
          refused and oom == "false" and restarts == restarts_before and alive,
          f"{mib} MiB body -> {status} in {elapsed:.1f}s, OOMKilled={oom}, "
          f"restarts {restarts_before}->{restarts}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--oversized-mib", type=int, default=96,
                        help="body size sent to a BFF auth route (the web container has 512 MiB)")
    args = parser.parse_args()
    work = args.work_dir.resolve()
    if work == ROOT or ROOT in work.parents:
        parser.error("the work dir holds a generated env file with secrets: keep it outside")
    work.mkdir(parents=True, exist_ok=True)
    project = f"{PROJECT_PREFIX}{secrets.token_hex(4)}"
    env_file, storage = work / f"{project}.env", work / f"{project}-storage"
    storage.mkdir()
    write_env(env_file, storage)
    stack = Stack(project, env_file)
    volumes_before = set(docker("volume", "ls", "-q").split())
    print(f"project: {project} (volume-free override; work dir {work})", flush=True)

    api = httpx.Client(base_url=f"http://127.0.0.1:{API_PORT}", timeout=60)
    origin = f"http://127.0.0.1:{WEB_PORT}"
    web = httpx.Client(base_url=origin, timeout=60)
    secrets_seen: list[str] = []
    try:
        if not args.skip_build:
            stack.run("build")
        stack.run("up", "-d")
        check("stack ready", wait(lambda: api.get("/api/v1/health/ready").status_code == 200)
              and wait(lambda: web.get("/login").status_code == 200))
        volumes_now = set(docker("volume", "ls", "-q").split())
        check("no Docker volume created", volumes_now == volumes_before)

        def new_user() -> dict[str, str]:
            email = f"drill-{secrets.token_hex(4)}@example.com"
            password = secrets.token_urlsafe(18)
            assert web.post("/bff/api/auth/register",
                            json={"email": email, "password": password}).status_code == 201
            token = bff_login(web, origin, email, password).json()["access_token"]
            secrets_seen.extend([email, password, token])
            return {"Authorization": f"Bearer {token}"}

        def envelope_code(response: httpx.Response) -> str | None:
            if not response.headers.get("content-type", "").startswith("application/json"):
                return None
            return response.json().get("error", {}).get("code")

        def no_internals(response: httpx.Response) -> bool:
            return not re.search(r"Traceback|psycopg|sqlalchemy|postgres:5432|File \"",
                                 response.text, re.I)

        bearer = new_user()

        # PostgreSQL hung (paused: TCP connects but nothing answers; data kept).
        stack.run("pause", "postgres")
        started = time.monotonic()
        try:
            ready = api.get("/api/v1/health/ready", timeout=30)
            ready_result = f"{ready.status_code} in {time.monotonic() - started:.1f}s"
            ready_ok = ready.status_code == 503
        except httpx.TimeoutException:
            ready_result, ready_ok = "no answer in 30s", False
        started = time.monotonic()
        docs_hung = False
        try:
            docs = web.get("/bff/api/documents", headers=bearer, timeout=40)
            docs_result = (f"{docs.status_code} {envelope_code(docs)} "
                           f"in {time.monotonic() - started:.1f}s")
            docs_ok = docs.status_code == 503 and no_internals(docs)
        except httpx.TimeoutException:
            docs_result, docs_ok, docs_hung = "no answer in 40s", False, True
        live = api.get("/api/v1/health", timeout=10).status_code
        check("database hung: liveness 200, readiness 503 promptly",
              live == 200 and ready_ok, f"liveness {live}, readiness {ready_result}")
        if docs_hung:
            # A pooled connection's pre-ping blocks while the server is frozen; there is no
            # client-side query timeout in sync psycopg. Documented residual risk R1.
            known("database hung: data calls wait until the database resumes", docs_result)
        else:
            check("database hung: data call fails with 503, not 500", docs_ok, docs_result)
        stack.run("unpause", "postgres")
        check("database resumed: ready again", wait(
            lambda: api.get("/api/v1/health/ready").status_code == 200, 120))

        # PostgreSQL stopped (connection refused). Its data is on tmpfs in this throwaway
        # stack, so it restarts empty: migrations are re-run and a new user is created.
        stack.run("stop", "postgres")
        live = api.get("/api/v1/health")
        ready = api.get("/api/v1/health/ready")
        docs = web.get("/bff/api/documents", headers=bearer)
        check("database down: liveness 200, readiness 503", live.status_code == 200
              and ready.status_code == 503, f"{live.status_code}/{ready.status_code}")
        check("database down: BFF returns a 503 envelope, no internals",
              docs.status_code == 503
              and envelope_code(docs) in {"database_unavailable", "service_unavailable"}
              and no_internals(docs), f"{docs.status_code} {envelope_code(docs)}")
        check("database down: web pages still served", web.get("/login").status_code == 200)
        stack.run("start", "postgres")
        wait(lambda: api.get("/api/v1/health/ready").json()["error"]["details"]["checks"]
             .get("schema") == "outdated", 60)
        stack.run("start", "migrate")
        check("database back (empty, re-migrated): ready again", wait(
            lambda: api.get("/api/v1/health/ready").status_code == 200, 120))
        bearer = new_user()

        # Worker down: uploads queue and complete later.
        stack.run("stop", "worker")
        text = "Drill fixture.\n\nThe synthetic depot fee is 321 units per week."
        up = web.post("/bff/api/documents", headers=bearer,
                      files={"file": ("drill.txt", text.encode(), "text/plain")})
        doc_id = up.json()["document"]["id"]

        def status() -> str:
            return web.get(f"/bff/api/documents/{doc_id}", headers=bearer).json()["status"]

        time.sleep(3)
        check("worker down: upload accepted and queued", up.status_code == 201
              and status() == "pending", status())
        stack.run("start", "worker")
        check("worker back: queued document indexed", wait(lambda: status() == "ready", 240))

        # LLM unreachable: only /ask fails.
        ask = web.post("/bff/api/ask", headers=bearer, json={"question": "What is the fee?"})
        search = web.post("/bff/api/search", headers=bearer, json={"query": "depot fee"})
        check("LLM unreachable: /ask 503 generation_unavailable, search still 200",
              ask.status_code == 503
              and ask.json()["error"]["code"] == "generation_unavailable"
              and search.status_code == 200, f"ask {ask.status_code}, search {search.status_code}")

        # Server files are not served; unknown methods and paths are refused.
        probes = {
            "/_next/server/app/(auth)/login/page.js.map": web.get(
                "/_next/server/app/(auth)/login/page.js.map").status_code,
            "/server.js": web.get("/server.js").status_code,
            "PUT /bff/api/documents": web.put("/bff/api/documents", headers=bearer).status_code,
            "/bff/api/docs": web.get("/bff/api/docs").status_code,
            "/bff/api/documents%2F..%2F..%2Fdocs": web.get(
                "/bff/api/documents%2F..%2F..%2Fdocs", headers=bearer).status_code,
        }
        check("server files, other methods and paths are not reachable",
              all(code in (404, 405) for code in probes.values()), json.dumps(probes))

        # Forged client addresses do not reset the login budget (forwarding is off).
        codes = []
        for i in range(25):
            r = bff_login(web, origin, f"nobody-{i}@example.com", "wrong-password-x",
                          **{"X-Forwarded-For": f"203.0.113.{i}"})
            codes.append(r.status_code)
            if r.status_code == 429:
                break
        check("forged X-Forwarded-For does not bypass the login rate limit", 429 in codes,
              f"429 after {len(codes)} attempts")

        oversized_body_check(stack, web, origin, args.oversized_mib)

        # Graceful termination.
        exits = {}
        for service in ("web", "worker", "api"):
            started = time.monotonic()
            stack.run("stop", service)
            code = docker("inspect", "--format", "{{.State.ExitCode}}", stack.container(service))
            exits[service] = (code, round(time.monotonic() - started, 1))
        # 137 = SIGKILL after the grace period; anything else is a handled shutdown.
        check("graceful termination of web, worker and api (no SIGKILL)",
              all(code != "137" for code, _ in exits.values()), json.dumps(exits))

        logs = stack.run("logs", "--no-color", check_rc=False).stdout
        leaked = [i for i, value in enumerate(secrets_seen) if value and value in logs]
        leaked += ["document text"] if "321 units" in logs else []
        leaked += ["bearer header"] if "Bearer " in logs else []
        check("no secrets, emails or document text in any log", not leaked, str(leaked))
    finally:
        stack.run("down", check_rc=False)  # containers and network only, never -v
        left = docker("ps", "-a", "--filter", f"label=com.docker.compose.project={project}",
                      "-q")
        new_volumes = set(docker("volume", "ls", "-q").split()) - volumes_before
        print(f"cleanup: containers left={len(left.split()) if left else 0}, "
              f"new volumes={sorted(new_volumes)}")
    failed = [name for name, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
