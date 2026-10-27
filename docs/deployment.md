# DocMind deployment and operations (Phase 9)

Phase 11 added the web frontend to this stack, backup tooling and a release checklist: see
[production-deployment.md](production-deployment.md) for the current guide.

This is a local, single-host deployment for evaluation and portfolio use. It is **not**
certified production-ready: that needs a security review, HTTPS in front, monitoring, and
operational experience with real load. Known risks are listed at the end.

## Architecture

```text
                 127.0.0.1:8080 (local only)
                        │
                 ┌──────┴──────┐       ┌──────────────┐
  network egress │     api     │       │    worker    │  same image, two commands
                 │  uvicorn    │       │ python -m    │  non-root uid 10001
                 └──────┬──────┘       │  app.worker  │  read-only root fs, cap_drop ALL
                        │              └──────┬───────┘
  network db     ───────┼─────────────────────┼──────────────  internal: no outside route
  (internal)            │                     │
                 ┌──────┴──────┐       ┌──────┴───────┐
                 │  postgres   │       │ ollama       │  optional profile "ollama"
                 │  pgvector   │       │ (no port)    │
                 └─────────────┘       └──────────────┘
   one-shot "migrate" (alembic upgrade head) runs before api and worker start
   volumes: pgdata, storage (uploaded originals), hf-cache (embedding model), ollama-models
```

- PostgreSQL is the only stateful dependency: data, the indexing job queue
  (`FOR UPDATE SKIP LOCKED`), refresh sessions and rate-limit counters. No Redis, Celery
  or Kubernetes: nothing measured needs them yet.
- The API and the worker never run migrations. `migrate` runs once per `up`; api and worker
  wait for it (`service_completed_successfully`), and both re-check the schema themselves:
  the API reports not-ready and the worker exits (code 3) when the database is not at the
  Alembic head of the code. A failed migration therefore keeps both down.
- Nothing is published except the API on `127.0.0.1`. PostgreSQL has no port at all.

## Build and start (Windows PowerShell)

```powershell
docker build -t docmind:local .                      # or let compose build it
Copy-Item deploy\.env.example deploy\.env            # git-ignored
notepad deploy\.env                                  # set POSTGRES_PASSWORD and DOCMIND_JWT_SECRET_KEY
# generate a secret (PowerShell 5.1):
$b = New-Object byte[] 48; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); [Convert]::ToBase64String($b)

docker compose -f deploy/compose.yaml --env-file deploy/.env up -d --build
docker compose -f deploy/compose.yaml --env-file deploy/.env ps
Invoke-RestMethod http://127.0.0.1:8080/api/v1/health/ready
docker compose -f deploy/compose.yaml --env-file deploy/.env logs -f api worker
docker compose -f deploy/compose.yaml --env-file deploy/.env down     # keeps the volumes
```

The project name is `docmind-stack`, distinct from the development compose file in the
repository root (`docmind`, PostgreSQL only). Never use `down -v` unless you intend to
delete that stack's database and documents.

### Model cache

The embedding model (`intfloat/multilingual-e5-small`, about 470 MB) is not in the image.
By default containers are offline (`HF_HUB_OFFLINE=1`) and expect it in the `hf-cache`
volume. Either allow one download (`DOCMIND_EMBEDDING_ALLOW_DOWNLOAD=true`,
`HF_HUB_OFFLINE=0` for the first start), or mount an existing host cache read-only as the
smoke test does (`deploy/compose.smoke.yaml`).

### LLM (optional)

Generation is `disabled` by default; search and uploads never depend on it and readiness
does not check it.

- Containerized Ollama: `--profile ollama`, `DOCMIND_GENERATION_BACKEND=openai-compatible`,
  `DOCMIND_GENERATION_BASE_URL=http://ollama:11434/v1`, then pull a model once
  (`docker compose ... exec ollama ollama pull qwen2.5:3b-instruct`, about 1.9 GB). The
  image itself is about 8.4 GB (CUDA libraries); GPU passthrough is not configured here.
- Ollama on the Windows host: `DOCMIND_GENERATION_BASE_URL=http://host.docker.internal:11434/v1`.
  Docker Desktop routes `host.docker.internal` to the host; the host Ollama must listen
  where Docker Desktop can reach it (by default `127.0.0.1:11434` works through Docker
  Desktop's host networking; if not, set `OLLAMA_HOST=0.0.0.0` and firewall the port).

## Configuration added in Phase 9

| Variable | Default | Purpose |
|---|---|---|
| `DOCMIND_REFRESH_TOKEN_TTL_DAYS` | `7` | Idle lifetime of a refresh token |
| `DOCMIND_SESSION_MAX_AGE_DAYS` | `30` | Absolute lifetime of a login session |
| `DOCMIND_RATE_LIMIT_ENABLED` | `true` | Auth rate limits on/off |
| `DOCMIND_RATE_LIMIT_LOGIN_PER_IP` | `20/60` | Login attempts per client address per window (requests/seconds) |
| `DOCMIND_RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL` | `10/900` | Failed logins per account |
| `DOCMIND_RATE_LIMIT_REGISTER_PER_IP` | `10/3600` | Registrations per address |
| `DOCMIND_RATE_LIMIT_REFRESH_PER_IP` | `60/60` | Token refreshes per address |
| `DOCMIND_TRUSTED_PROXIES` | empty | Proxies whose `X-Forwarded-For` is believed (IPs/CIDRs) |
| `DOCMIND_ALLOWED_HOSTS` | empty | Accepted Host headers; **required in prod** |
| `DOCMIND_CORS_ALLOWED_ORIGINS` | empty | Browser origins allowed (CORS off when empty) |
| `DOCMIND_HSTS_ENABLED` | `false` | Send HSTS (only behind HTTPS) |
| `DOCMIND_DOCS_ENABLED` | unset | `/docs` and `/openapi.json`; unset = off in prod |
| `DOCMIND_LOG_FORMAT` | `json` | `json` or `text` |
| `DOCMIND_WORKER_HEARTBEAT_FILE` | unset (image: `/tmp/docmind-worker-heartbeat`) | Worker liveness file |
| `DOCMIND_MAINTENANCE_INTERVAL_S` | `3600` | Cleanup of expired sessions and counters |

Prod refuses to start with `debug=true`, without `DOCMIND_ALLOWED_HOSTS` (or with `*`),
with CORS `*`, or without a 32-byte `DOCMIND_JWT_SECRET_KEY`.

## Sessions: refresh tokens and logout

```text
POST /auth/login    -> access token (JWT, 15 min, carries the session id "sid")
                       + refresh token (opaque, 256 bits, stored only as SHA-256)
POST /auth/refresh  -> spends the refresh token, returns a new pair (rotation)
                       a spent token presented again = reuse -> the whole session is revoked
POST /auth/logout   -> revokes this session ({"all_sessions": true}: every session)
GET  /auth/sessions -> your active sessions
```

- Every authenticated request loads the session by `sid` and requires it active, so
  logout, reuse detection, `python -m app.admin deactivate` and `set-password` revoke access
  tokens **immediately**, not after their 15 minutes. Cost: one primary-key lookup per
  request.
- Concurrent refreshes with one token are serialized by row locks; the first wins, the
  second is treated as reuse and revokes the session. A client must not refresh in
  parallel with the same token.
- Limits: refresh tokens expire after 7 days of disuse; a session ends 30 days after login
  whatever happens. Expired rows are pruned by the worker.
- Threat model: protects against a stolen refresh token being used in parallel with the
  owner (reuse revokes both), a stolen access token after logout, and database leaks of
  refresh tokens (hashes only). It does not protect a token stolen and used before the
  owner's next refresh, tokens leaked from the client device, or a compromised server.

## Rate limiting

PostgreSQL fixed-window counters (`rate_limits`): one atomic upsert per limited request,
shared by every API process, keys hashed (no email or address in clear), pruned by the
worker. 429 `rate_limited` with `Retry-After`. Per-account limits count only failed logins,
so other people's attempts cannot spend an owner's budget with correct passwords... but
they can spend the failure budget and lock the account for the window: that trade-off is
inherent to any per-account limit.

Client addresses: the TCP peer, unless the peer is in `DOCMIND_TRUSTED_PROXIES`; then the
rightmost `X-Forwarded-For` hop that is not a trusted proxy. Uvicorn's own proxy-header
handling is disabled so there is only one trust decision. Behind a reverse proxy, set
`DOCMIND_TRUSTED_PROXIES` to the proxy's address. This does not stop distributed attacks
from many addresses; a reverse proxy or WAF is the place for that.

## Logs, health, recovery

- Logs: one JSON object per line (ts UTC, level, service api/worker, logger, msg,
  request_id, http_method, http_route template, http_status, elapsed_ms). Never: tokens,
  passwords, emails, Authorization headers, query strings, document text, exception
  messages. `X-Request-ID` is accepted only if it is 1-64 chars of `[A-Za-z0-9._-]` and is
  returned on every response.
- `/api/v1/health`: liveness, no dependencies. `/api/v1/health/ready`: database reachable
  and schema at head (503 otherwise). Neither loads the embedding model or calls the LLM.
- Worker: healthcheck reads a heartbeat file (`python -m app.worker healthcheck`); SIGTERM
  finishes the current job; a killed worker's job is reclaimed after
  `DOCMIND_JOB_LEASE_TIMEOUT_S` (default 1800 s), retried up to 3 times, then the document
  is marked failed. Queued documents survive restarts (they are rows, not memory).

## Migrations, backup and recovery

```powershell
# backup (custom format, restorable with pg_restore)
docker compose -f deploy/compose.yaml --env-file deploy/.env exec postgres pg_dump -U docmind -d docmind -Fc -f /tmp/docmind.dump
docker compose -f deploy/compose.yaml --env-file deploy/.env cp postgres:/tmp/docmind.dump "$env:USERPROFILE\.docmind\backups\docmind-$(Get-Date -Format yyyyMMdd-HHmm).dump"
# the uploaded originals live in the "storage" volume: back it up together with the dump
# upgrade: new image, then
docker compose -f deploy/compose.yaml --env-file deploy/.env up -d --build   # migrate runs first
# restore into an EMPTY database only (never over live data)
docker compose -f deploy/compose.yaml --env-file deploy/.env exec postgres pg_restore -U docmind -d docmind --clean --if-exists /tmp/docmind.dump
```

Read new migrations before applying them (`alembic upgrade head --sql`). Nothing in this
stack ever drops or resets a database automatically.

## CI (`.github/workflows/ci.yml`)

Jobs: `lint` (ruff, whitespace), `test` (unit + PostgreSQL/pgvector integration tests,
Alembic single-head check; the integration suite itself upgrades, downgrades and checks
model drift), `docker` (build without pushing, non-root check, compose validation),
`audit` (pip-audit of both lock files; advisory). Actions are pinned to commit SHAs,
`permissions: contents: read`, checkout without persisted credentials, no secrets used, no
images published, no deployment. Real-model tests never run in CI. Required GitHub
settings: none; optionally make `lint`, `test` and `docker` required checks for `main`.

## End-to-end smoke test

```powershell
# the env file holds generated secrets: keep it outside the repository
.\.venv\Scripts\python.exe scripts\smoke_test.py --env-file "$env:USERPROFILE\.docmind\smoke.env"
docker compose -p docmind-smoke -f deploy/compose.yaml -f deploy/compose.smoke.yaml --env-file "$env:USERPROFILE\.docmind\smoke.env" ps -a
```

It builds the image and runs the stack as project `docmind-smoke` (volumes
`docmind-smoke_pgdata`, `_storage`, `_hf-cache`; API on `127.0.0.1:8181`), with the fake
generator and the host's Hugging Face cache mounted read-only. Each run registers fresh
accounts, so it can be repeated on the same volumes. It stops the stack at the end and never
deletes volumes. The 22 checks: stack ready, migration exit code 0, liveness, readiness
(database and schema), registration and login of Alice and Bob, indexing of their documents
by the worker container, semantic search, hybrid search and `/ask` seeing only the caller's
document, 404 for the other user's and for unknown documents (read, delete, `/ask`),
refresh rotation, replay revoking the session, logout revoking access and refresh tokens
at once, 429 on a login burst, readiness and data after restarting api and worker, an upload
queued while the worker is stopped, a job whose worker is SIGKILLed mid-indexing being
reclaimed and completed, no passwords, tokens, emails, `Bearer` or document text in the
logs, and every api/worker log line being JSON.

Verified on 2026-10-27 (Windows 11, Docker Desktop 29.9.2): 22/22 checks passed. The first
run found 11 non-JSON log lines (third-party progress bars while loading the model, and an
onnxruntime telemetry warning: onnxruntime is a PyMuPDF dependency). The image now sets
`HF_HUB_DISABLE_PROGRESS_BARS=1` and `ORT_DISABLE_TELEMETRY=1`; later runs: 233/233 JSON lines.

## Verified checks (2026-10-27)

- `actionlint` 1.7.7 on `.github/workflows/ci.yml`: no findings (run without shellcheck, so
  shell snippets were not linted). The workflow has not run on GitHub yet.
- `pip-audit` 2.9.0 on `requirements/dev.txt` and `requirements/runtime.txt`: no known
  vulnerabilities. `torch 2.14.1+cpu` cannot be audited (the `+cpu` build is not on PyPI),
  so the runtime audit is not `--strict`; check PyTorch advisories by hand when upgrading.

## Future HTTPS deployment

Terminate TLS in a reverse proxy (Caddy, nginx, Traefik) on the same host, forward to
`127.0.0.1:8080`, then set `DOCMIND_ALLOWED_HOSTS` to the public name,
`DOCMIND_TRUSTED_PROXIES` to the proxy address and `DOCMIND_HSTS_ENABLED=true`. Add rate
limits and request size limits at the proxy too. The app itself never handles TLS.

## Known production risks

- Single host, single PostgreSQL, no replication, no automated backups.
- HS256: anyone who can read the JWT secret can mint tokens; rotate it if leaked (all
  sessions end).
- No email verification, password reset or MFA; registration is open.
- Rate limits are per address and per account, not a defence against distributed attacks.
- Embedding queries run inside each API process (about 0.5 GB RAM per process).
- The image is large (about 2 GB, mostly PyTorch).
- Dependency vulnerabilities: see the audit job; pinned versions need regular updates.
- Containerized Ollama runs as root inside its container (upstream image) and has no GPU
  configuration here.
