"""End-to-end smoke test of the containerized stack, on isolated disposable resources.

    python scripts/smoke_test.py --env-file <path outside the repo>

Runs `docker compose -p docmind-smoke -f deploy/compose.yaml -f deploy/compose.smoke.yaml`
(its own project name, so its containers and volumes are docmind-smoke_*), then checks:
build, migrations, liveness/readiness, registration and login of two users, uploads,
indexing, semantic and hybrid search, /ask with the fake generator, refresh rotation,
logout and replay rejection, cross-user isolation, rate limiting, restart persistence,
recovery of a job interrupted by a killed worker, the web app and its BFF (pages, CSP,
cookie session, calls to the API over the internal network), and leak-free logs.

Throwaway release check (no Docker volumes at all, see deploy/compose.release-check.yaml):

    python scripts/smoke_test.py --project docmind-relcheck --override compose.release-check.yaml `
        --port 8281 --web-port 3281 --storage-dir <empty dir> --env-file <path> --down

Secrets: if the env file does not exist it is created with random POSTGRES_PASSWORD and
DOCMIND_JWT_SECRET_KEY; they are never printed. By default the stack is stopped at the end
WITHOUT deleting volumes; --down removes the containers and network instead (never volumes,
and refused for the developer, deployment and smoke projects). Remove smoke volumes
yourself once you have checked their names:
    docker compose -p docmind-smoke -f deploy/compose.yaml -f deploy/compose.smoke.yaml \
        --env-file <same env file> down -v
"""

import argparse
import contextlib
import json
import re
import secrets
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "docmind-smoke"
# Projects whose containers this script must never remove (--down).
PROTECTED_PROJECTS = frozenset({"docmind", "docmind-stack", "docmind-smoke"})
PASSWORD = {"alice": secrets.token_urlsafe(18), "bob": secrets.token_urlsafe(18)}
# Fresh accounts per run, so the test can be repeated on the same volumes.
EMAIL = {user: f"{user}-{secrets.token_hex(4)}@example.com" for user in PASSWORD}
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{(' - ' + detail) if detail else ''}", flush=True)


class Stack:
    def __init__(self, env_file: Path, project: str = PROJECT,
                 override: str = "compose.smoke.yaml") -> None:
        self.base = [
            "docker", "compose", "-p", project,
            "-f", str(ROOT / "deploy" / "compose.yaml"),
            "-f", str(ROOT / "deploy" / override),
            "--env-file", str(env_file),
        ]

    def run(self, *args: str, check_rc: bool = True, capture: bool = False):
        return subprocess.run(
            [*self.base, *args], cwd=ROOT, check=check_rc, text=True, encoding="utf-8",
            capture_output=capture,
        )

    def logs(self) -> str:
        return self.run("logs", "--no-color", capture=True, check_rc=False).stdout


def write_env(path: Path, port: int, web_port: int = 3181,
              storage_dir: Path | None = None) -> None:
    if path.exists():
        return
    hub = Path.home() / ".cache" / "huggingface" / "hub"
    extra = [f"CHECK_STORAGE_DIR={storage_dir}"] if storage_dir else []
    path.write_text(
        "\n".join([
            "POSTGRES_USER=docmind",
            f"POSTGRES_PASSWORD={secrets.token_urlsafe(24)}",
            "POSTGRES_DB=docmind",
            f"DOCMIND_JWT_SECRET_KEY={secrets.token_urlsafe(48)}",
            "DOCMIND_ENVIRONMENT=test",
            "DOCMIND_ALLOWED_HOSTS=localhost,127.0.0.1",
            f"DOCMIND_API_PORT={port}",
            f"DOCMIND_WEB_PORT={web_port}",
            "DOCMIND_GENERATION_BACKEND=fake",
            "DOCMIND_JOB_LEASE_TIMEOUT_S=30",
            f"HOST_HF_HUB={hub}",
            *extra,
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


def wait_web(client: httpx.Client, timeout_s: float = 120) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if client.get("/login").status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(2)
    return False


def refresh_cookie(response: httpx.Response) -> tuple[str | None, str]:
    header = response.headers.get("set-cookie", "")
    match = re.search(r"docmind_refresh=([^;]*)", header)
    return (match.group(1) if match else None), header.lower()


def web_checks(web: httpx.Client, origin: str) -> list[str]:
    """The containerized Next.js app: pages, CSP, BFF session and the internal API URL.
    Returns the secrets it used, for the log leak scan."""
    page = web.get("/login")
    csp = page.headers.get("content-security-policy", "")
    nonce = re.search(r"'nonce-([^']+)'", csp)
    scripts = re.findall(r"<script\b[^>]*>", page.text)
    check("web: sign-in page with a nonce CSP on every script",
          page.status_code == 200 and nonce is not None and "unsafe-inline" not in csp
          and bool(scripts) and all(f'nonce="{nonce.group(1)}"' in s for s in scripts),
          f"{len(scripts)} scripts")

    email = f"web-{secrets.token_hex(4)}@example.com"
    password = secrets.token_urlsafe(18)
    creds = {"email": email, "password": password}
    same_origin = {"Origin": origin}
    reg = web.post("/bff/api/auth/register", json=creds)
    login = web.post("/bff/auth/login", json=creds, headers=same_origin)
    cookie, flags = refresh_cookie(login)
    body = login.json() if login.status_code == 200 else {}
    check("web: register and sign in through the BFF (API reached as http://api:8000)",
          reg.status_code == 201 and login.status_code == 200
          and set(body) == {"access_token", "expires_in"})
    check("web: refresh token only in an HttpOnly, SameSite=Strict, Secure cookie",
          bool(cookie) and "httponly" in flags and "samesite=strict" in flags
          and "secure" in flags)
    bearer = {"Authorization": f"Bearer {body.get('access_token', '')}"}
    me = web.get("/bff/api/auth/me", headers=bearer)
    check("web: API call through the BFF proxy",
          me.status_code == 200 and me.json().get("email") == email)
    cross = web.post("/bff/auth/login", json=creds, headers={"Origin": "https://evil.example"})
    check("web: cross-site sign-in refused", cross.status_code == 403)
    blocked = web.post("/bff/api/auth/refresh", json={"refresh_token": "x"})
    check("web: token endpoints not reachable through the data proxy", blocked.status_code == 404)

    rotated = web.post("/bff/auth/refresh", json={},
                       headers={**same_origin, "Cookie": f"docmind_refresh={cookie}"})
    new_cookie, _ = refresh_cookie(rotated)
    check("web: refresh rotates the cookie",
          rotated.status_code == 200 and bool(new_cookie) and new_cookie != cookie)
    token = rotated.json().get("access_token", "") if rotated.status_code == 200 else ""
    out = web.post("/bff/auth/logout", json={}, headers={
        **same_origin, "Authorization": f"Bearer {token}",
        "Cookie": f"docmind_refresh={new_cookie}"})
    after = web.get("/bff/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    check("web: sign-out revokes the session",
          out.status_code == 204 and after.status_code == 401)
    return [password, body.get("access_token", ""), cookie or "", new_cookie or "", email]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8181)
    parser.add_argument("--web-port", type=int, default=3181)
    parser.add_argument("--project", default=PROJECT)
    parser.add_argument("--override", default="compose.smoke.yaml",
                        help="override file in deploy/ (compose.release-check.yaml: no volumes)")
    parser.add_argument("--storage-dir", type=Path,
                        help="host directory for uploads (CHECK_STORAGE_DIR, release check)")
    parser.add_argument("--down", action="store_true",
                        help="remove containers and network at the end (never volumes)")
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args()
    if ROOT in args.env_file.resolve().parents:
        parser.error("keep the smoke env file (it holds secrets) outside the repository")
    if args.down and args.project in PROTECTED_PROJECTS:
        parser.error(f"--down is refused for the {args.project!r} project")
    if args.storage_dir:
        args.storage_dir.mkdir(parents=True, exist_ok=True)
    write_env(args.env_file, args.port, args.web_port, args.storage_dir)
    stack = Stack(args.env_file, args.project, args.override)
    api = httpx.Client(base_url=f"http://127.0.0.1:{args.port}", timeout=120)
    web_origin = f"http://127.0.0.1:{args.web_port}"
    web = httpx.Client(base_url=web_origin, timeout=120)

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

    # The web container: started after the API is healthy; talks to it over the app network.
    check("web app ready", wait_web(web))
    web_secrets = web_checks(web, web_origin)

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
        *((f"web secret {i}", value) for i, value in enumerate(web_secrets) if value),
    ) if value in logs]
    check("logs contain no passwords, tokens, emails or document text", not leaks, str(leaks))
    project = args.project
    json_lines = [line.split("|", 1)[-1].strip() for line in logs.splitlines()
                  if line.startswith(("api", "worker", f"{project}-api", f"{project}-worker"))]
    parsed = 0
    for line in json_lines:
        with contextlib.suppress(ValueError):
            parsed += isinstance(json.loads(line), dict)
    check("api and worker logs are JSON", bool(json_lines) and parsed == len(json_lines),
          f"{parsed}/{len(json_lines)}")

    # 19: stop the isolated stack only; volumes are kept. --down also removes its containers
    # and network (deliberately never with -v).
    stack.run("down" if args.down else "stop")
    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
