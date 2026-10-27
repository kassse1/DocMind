"""End-to-end smoke test of the containerized stack, on isolated disposable resources.

    python scripts/smoke_test.py --env-file <path outside the repo>

Runs `docker compose -p docmind-smoke -f deploy/compose.yaml -f deploy/compose.smoke.yaml`
(its own project name, so its containers and volumes are docmind-smoke_*), then checks:
build, migrations, liveness/readiness, registration and login of two users, uploads,
indexing, semantic and hybrid search, /ask with the fake generator, refresh rotation,
logout and replay rejection, cross-user isolation, rate limiting, restart persistence,
recovery of a job interrupted by a killed worker, and leak-free logs.

Secrets: if the env file does not exist it is created with random POSTGRES_PASSWORD and
DOCMIND_JWT_SECRET_KEY; they are never printed. The stack is stopped at the end WITHOUT
deleting volumes; remove them yourself once you have checked their names:
    docker compose -p docmind-smoke -f deploy/compose.yaml -f deploy/compose.smoke.yaml \
        --env-file <same env file> down -v
"""

import argparse
import contextlib
import json
import secrets
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "docmind-smoke"
PASSWORD = {"alice": secrets.token_urlsafe(18), "bob": secrets.token_urlsafe(18)}
# Fresh accounts per run, so the test can be repeated on the same volumes.
EMAIL = {user: f"{user}-{secrets.token_hex(4)}@example.com" for user in PASSWORD}
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{(' - ' + detail) if detail else ''}", flush=True)


class Stack:
    def __init__(self, env_file: Path) -> None:
        self.base = [
            "docker", "compose", "-p", PROJECT,
            "-f", str(ROOT / "deploy" / "compose.yaml"),
            "-f", str(ROOT / "deploy" / "compose.smoke.yaml"),
            "--env-file", str(env_file),
        ]

    def run(self, *args: str, check_rc: bool = True, capture: bool = False):
        return subprocess.run(
            [*self.base, *args], cwd=ROOT, check=check_rc, text=True, encoding="utf-8",
            capture_output=capture,
        )

    def logs(self) -> str:
        return self.run("logs", "--no-color", capture=True, check_rc=False).stdout


def write_env(path: Path, port: int) -> None:
    if path.exists():
        return
    hub = Path.home() / ".cache" / "huggingface" / "hub"
    path.write_text(
        "\n".join([
            "POSTGRES_USER=docmind",
            f"POSTGRES_PASSWORD={secrets.token_urlsafe(24)}",
            "POSTGRES_DB=docmind",
            f"DOCMIND_JWT_SECRET_KEY={secrets.token_urlsafe(48)}",
            "DOCMIND_ENVIRONMENT=test",
            "DOCMIND_ALLOWED_HOSTS=localhost,127.0.0.1",
            f"DOCMIND_API_PORT={port}",
            "DOCMIND_GENERATION_BACKEND=fake",
            "DOCMIND_JOB_LEASE_TIMEOUT_S=30",
            f"HOST_HF_HUB={hub}",
        ]) + "\n",
        encoding="utf-8",
    )


def wait_ready(client: httpx.Client, timeout_s: float = 240) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if client.get("/api/v1/health/ready").status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(2)
    return False


def wait_status(client: httpx.Client, headers, doc_id: str, wanted: set[str], timeout_s=240):
    deadline = time.monotonic() + timeout_s
    status = None
    while time.monotonic() < deadline:
        status = client.get(f"/api/v1/documents/{doc_id}", headers=headers).json().get("status")
        if status in wanted:
            return status
        time.sleep(1)
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8181)
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args()
    if ROOT in args.env_file.resolve().parents:
        parser.error("keep the smoke env file (it holds secrets) outside the repository")
    write_env(args.env_file, args.port)
    stack = Stack(args.env_file)
    api = httpx.Client(base_url=f"http://127.0.0.1:{args.port}", timeout=120)

    # 1-5: build, postgres, migrations, api and worker, liveness and readiness.
    if not args.skip_build:
        stack.run("build")
    stack.run("up", "-d")
    check("stack ready (postgres healthy, migrate completed, api ready)", wait_ready(api))
    migrate = stack.run("ps", "-a", "--format", "json", "migrate", capture=True).stdout
    check("migrate exited 0", '"ExitCode":0' in migrate.replace(" ", ""))
    check("liveness", api.get("/api/v1/health").status_code == 200)
    ready = api.get("/api/v1/health/ready").json()
    check("readiness checks database and schema", ready.get("checks") ==
          {"database": "ok", "schema": "ok"}, json.dumps(ready.get("checks")))

    # 6-7: register and log in.
    tokens, headers = {}, {}
    for user in ("alice", "bob"):
        email = EMAIL[user]
        reg = api.post("/api/v1/auth/register", json={"email": email, "password": PASSWORD[user]})
        log = api.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD[user]})
        check(f"register + login {user}", reg.status_code == 201 and log.status_code == 200)
        tokens[user] = log.json()
        headers[user] = {"Authorization": f"Bearer {tokens[user]['access_token']}"}

    # 8-9: uploads and indexing by the worker container.
    docs = {
        "alice": ("lease.txt", "Warehouse lease.\n\nThe monthly rent is 450 000 tenge."),
        "bob": ("gym.txt", "Gym terms.\n\nA standard membership costs 45 dollars per month."),
    }
    ids = {}
    for user, (name, text) in docs.items():
        response = api.post("/api/v1/documents", headers=headers[user],
                            files={"file": (name, text.encode(), "text/plain")})
        ids[user] = response.json()["document"]["id"]
    for user in ids:
        status = wait_status(api, headers[user], ids[user], {"ready", "failed"})
        check(f"{user}'s document indexed", status == "ready", str(status))

    # 10-11: semantic and hybrid search, /ask (fake generator); 14: isolation.
    for user, other in (("alice", "bob"), ("bob", "alice")):
        q = {"query": "how much per month", "top_k": 10}
        sem = api.post("/api/v1/search", headers=headers[user], json=q).json()["results"]
        hyb = api.post("/api/v1/search/hybrid", headers=headers[user], json=q).json()["results"]
        ask = api.post("/api/v1/ask", headers=headers[user], json={"question": "Monthly cost?"})
        cited = {c["document_id"] for c in ask.json().get("citations", [])}
        check(f"{user}: search, hybrid and ask see only own document",
              {r["document_id"] for r in sem} == {ids[user]}
              and {r["document_id"] for r in hyb} == {ids[user]}
              and ask.status_code == 200 and cited <= {ids[user]})
        foreign = [
            api.get(f"/api/v1/documents/{ids[other]}", headers=headers[user]).status_code,
            api.delete(f"/api/v1/documents/{ids[other]}", headers=headers[user]).status_code,
            api.post("/api/v1/ask", headers=headers[user],
                     json={"question": "x", "document_ids": [ids[other]]}).status_code,
            api.get(f"/api/v1/documents/{uuid.uuid4()}", headers=headers[user]).status_code,
        ]
        check(f"{user}: foreign and missing documents are 404", foreign == [404] * 4, str(foreign))

    # 12-13: refresh rotation, logout, replay.
    first = tokens["alice"]["refresh_token"]
    rotated = api.post("/api/v1/auth/refresh", json={"refresh_token": first})
    check("refresh rotates", rotated.status_code == 200
          and rotated.json()["refresh_token"] != first)
    new = rotated.json()
    replay = api.post("/api/v1/auth/refresh", json={"refresh_token": first})
    after_replay = api.post("/api/v1/auth/refresh", json={"refresh_token": new["refresh_token"]})
    check("replayed refresh token revokes the session",
          replay.status_code == 401 and after_replay.status_code == 401)
    login = api.post("/api/v1/auth/login",
                     json={"email": EMAIL["alice"], "password": PASSWORD["alice"]}).json()
    headers["alice"] = {"Authorization": f"Bearer {login['access_token']}"}
    out = api.post("/api/v1/auth/logout", headers=headers["alice"])
    check("logout revokes access and refresh tokens immediately",
          out.status_code == 204
          and api.get("/api/v1/auth/me", headers=headers["alice"]).status_code == 401
          and api.post("/api/v1/auth/refresh",
                       json={"refresh_token": login["refresh_token"]}).status_code == 401)

    # 15: rate limiting (default 20 logins/min per address).
    codes = [api.post("/api/v1/auth/login", json={"email": EMAIL["bob"],
                                                  "password": PASSWORD["bob"]}).status_code
             for _ in range(22)]
    limited = [c for c in codes if c == 429]
    check("login burst is rate limited with 429", bool(limited), f"{codes.count(200)}x200, "
          f"{len(limited)}x429")
    headers["bob"] = {"Authorization": f"Bearer {tokens['bob']['access_token']}"}

    # 16-17: restart api and worker; data and sessions persist.
    stack.run("restart", "api", "worker")
    check("ready after restart", wait_ready(api))
    check("bob's token and document survive the restart",
          api.get(f"/api/v1/documents/{ids['bob']}", headers=headers["bob"]).status_code == 200)

    # Interrupted job: the worker is SIGKILLed mid-indexing; after the lease another worker
    # run reclaims it. Nothing queued is lost.
    stack.run("stop", "worker")
    big = "\n\n".join(f"Clause {i}: the tenant pays the rent before day {i % 28 + 1}."
                      for i in range(1500))
    up = api.post("/api/v1/documents", headers=headers["bob"],
                  files={"file": ("big.txt", big.encode(), "text/plain")})
    big_id = up.json()["document"]["id"]
    check("upload queued while the worker is down",
          wait_status(api, headers["bob"], big_id, {"pending"}, 5) == "pending")
    stack.run("start", "worker")
    seen = wait_status(api, headers["bob"], big_id, {"processing", "ready"}, 180)
    if seen == "processing":
        stack.run("kill", "-s", "SIGKILL", "worker")
        time.sleep(2)
        stack.run("start", "worker")
    final = wait_status(api, headers["bob"], big_id, {"ready", "failed"}, 300)
    check("job interrupted by SIGKILL is reclaimed and completes",
          final == "ready", f"seen={seen} final={final}")

    # 18: no secrets or private data in the logs.
    logs = stack.logs()
    leaks = [name for name, value in (
        ("alice password", PASSWORD["alice"]), ("bob password", PASSWORD["bob"]),
        ("access token", tokens["bob"]["access_token"]),
        ("refresh token", tokens["bob"]["refresh_token"]), ("email", "@example.com"),
        ("authorization header", "Bearer "), ("document text", "450 000 tenge"),
    ) if value in logs]
    check("logs contain no passwords, tokens, emails or document text", not leaks, str(leaks))
    json_lines = [line.split("|", 1)[-1].strip() for line in logs.splitlines()
                  if line.startswith(("api", "worker", f"{PROJECT}-api", f"{PROJECT}-worker"))]
    parsed = 0
    for line in json_lines:
        with contextlib.suppress(ValueError):
            parsed += isinstance(json.loads(line), dict)
    check("api and worker logs are JSON", bool(json_lines) and parsed == len(json_lines),
          f"{parsed}/{len(json_lines)}")

    # 19: stop the isolated stack only; volumes are kept.
    stack.run("stop")
    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
