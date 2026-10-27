# Phase 12: final security and release audit

Date: 2026-10-27. Branch `phase-12-final-audit`, created from `phase-11-deployment-readiness`
at `306530c` (which contains `phase-10-frontend` `d590f17`). Everything is local; nothing
was pushed, published or deployed. This is an assessment, not a release.

Scope: the repository (backend, frontend BFF, Compose, CI, scripts) and its git history,
the locally built images, and throwaway Docker stacks. Out of scope: TLS and reverse-proxy
behaviour, load and denial-of-service testing beyond single oversized requests, the host OS,
third-party services, a full restore-and-cut-over.

## Decision

**READY WITH LIMITATIONS.**

| Use | Assessment | Conditions |
|---|---|---|
| Local demo / portfolio (loopback Compose stack) | **Ready** | Use `http://localhost:3080`, keep `deploy/.env` private. |
| Publishing the source code | **Ready** after the owner's own review | Secret scan below found no committed secret, dump or local data; `.env` files are ignored. Pushing is the owner's decision. |
| Production / internet deployment | **Not ready** | Needs TLS and a reverse proxy that sets `X-Forwarded-For` (R2), a decision on R1, off-site encrypted backups and a rehearsed restore-and-cut-over, monitoring, and green GitHub Actions. |

## Findings

| ID | Severity | Component | Evidence | Remediation | Status |
|---|---|---|---|---|---|
| F1 | Medium | frontend `safeNextPath` | `/login?next=/%09/evil.example`: the decoded tab passed the check (`starts with /`, not `//`, no `\`), and URL parsing strips tabs, so the browser resolved it to `http://evil.example` (reproduced with Node's WHATWG `URL`). An open redirect after sign-in, usable for phishing. | Resolve the target like the browser and require the same origin; refuse control characters and backslashes. Tests for tab, newline, CR LF, NUL, encoded forms. | Fixed `8aa87d3` |
| F2 | Medium | frontend BFF `/bff/auth/login`, `/logout` | `request.text()` read the whole body before the 4 KiB check. Failure drill on the unfixed image: a 640 MiB chunked body crashed the Next.js process; the `web` container restarted (restart count 0 → 1). One unauthenticated request takes the web app down. | Refuse an oversized `Content-Length` at once; read streamed bodies in chunks and cancel past 4 KiB (413 `payload_too_large`). Tests with an endless stream (≤ 8 KiB pulled, stream cancelled). | Fixed `eeac3f1`; drill after fix: 640 MiB and 96 MiB refused, restart count 0 → 0 |
| F3 | Medium | backend readiness | Drill, PostgreSQL paused: `/health/ready` gave no answer within 30 s. The check used a pooled connection whose pre-ping blocks in a read that neither `statement_timeout` (server side) nor `connect_timeout` (new connections only) covers; each probe leaves a worker thread hanging. | Readiness opens a fresh, unpooled connection bounded by the readiness timeout. Test with a TCP server that accepts and never answers. | Fixed `46df8a9`; drill after fix: 503 in 2.0 s |
| F4 | Low | backend documents router | Drill, PostgreSQL stopped: `GET /documents` returned 500 `internal_error`, while auth, search and ask return 503 `database_unavailable`. No internals leaked. | Documents router uses `DatabaseUnavailableRoute`; tests for all four routes and both error kinds; upload test updated (lost connection → 503). | Fixed `c8fc978`, `d956435` |
| R1 | Medium (residual) | backend database access | Drill, PostgreSQL paused: a data request through the BFF did not answer within 40 s. Pooled connections block while the server is frozen (the host kernel still acknowledges packets, so TCP timeouts do not fire); requests resume when the database does. Many such requests can exhaust uvicorn's thread pool. | Not changed in this phase: sync psycopg has no safe client-side query timeout, and a fix touches every request. Recommended: a request timeout at the reverse proxy, `tcp_user_timeout` and keepalives in the connection settings (covers network partitions, not a frozen process), alerting on readiness. | Open, documented; drill reports it as `KNOWN` |
| R2 | Medium (residual, availability) | rate limiting behind the BFF | By design the API sees the `web` container's address for every browser (forwarding off, `DOCMIND_TRUSTED_PROXIES` empty). Drill: forged `X-Forwarded-For` does not bypass the limit (429 after 21 attempts). Cost: the limits are shared by all users: 20 logins/min, 10 registrations/h, 60 refreshes/min, and a refresh happens on every page load. | See "Shared rate-limit budget" below. | Open, documented |
| I1 | Info | BFF CSRF check | `sameOriginJson` compares `Origin` with `X-Forwarded-Host` when present, a client-supplied header. Not exploitable from a browser: a cross-site request carrying it needs a CORS preflight the BFF never grants; non-browser clients need no CSRF. | Keep; prefer `Host` only once a reverse proxy is fixed. | Accepted |
| I2 | Info | web Host header | The web app has no Host allowlist (the API has one: a foreign Host gets 400). DNS rebinding could read pages, but the refresh cookie is bound to `localhost`, so no session data. | Add a Host allowlist at the reverse proxy. | Accepted |
| I3 | Info | CI `audit` job | `pip install pip-audit==2.9.0` without hashes in an advisory job (read-only token, no secrets). | Hash-pin when the job becomes blocking. | Accepted |
| I4 | Info | dependency audit | `torch 2.14.1+cpu` cannot be audited (not on PyPI). | Track PyTorch advisories by hand. | Accepted |
| I5 | Info | web image | Server-side source maps are present under `.next/server` (16 files). Not served: `/_next/server/...map` and `/server.js` answer 404 (drill). No maps in client assets. | None needed. | Verified |
| I6 | Info | dev environment | The local dev database password is short (8 characters); it is bound to 127.0.0.1 and not in any tracked file or commit. | Use a generated password if the dev stack is ever exposed. | Accepted |

Known by design and documented earlier: no TLS in the app, HS256 shared secret, open
registration without email verification or MFA, single host, the Ollama image runs as root.

### Shared rate-limit budget: recommended fix

Do not trust `X-Forwarded-For` from the browser. Put a reverse proxy (Caddy, nginx) in
front of `web` that **overwrites** the header with the TCP peer address (nginx:
`proxy_set_header X-Forwarded-For $remote_addr;`, not `$proxy_add_x_forwarded_for`), and
publish only the proxy. Then set `DOCMIND_BFF_FORWARD_CLIENT_IP=true` on `web` and the
`web` container's fixed address (or the `app` network subnet with a fixed range) in the
API's `DOCMIND_TRUSTED_PROXIES`. The API takes the rightmost hop that is not a trusted
proxy: the address the proxy wrote. Verify with the drill's forged-header check against
the proxy port before relying on it. Until then, the shared budget is the safe default.

## Secret and repository hygiene

- Scanners: gitleaks, trufflehog, detect-secrets, semgrep, bandit and trivy are not
  installed (not installed by this audit). Used instead: a pattern scan (private keys, AWS,
  GitHub, OpenAI/Anthropic and Slack tokens, JWTs, database URLs with passwords, secret
  assignments) over 283 tracked files and the added lines of all 87 commits, printing only
  location and type.
- Result: 15 current and 17 historical matches, all triaged with values masked: code that
  handles secrets (`secret = settings.jwt_secret_key...`), test constants (unreachable
  `127.0.0.1:1` URLs, fixture passwords), the `change-me` placeholder and the documented
  dev default. No credential.
- Cross-check: the 6 real secret values in the local env files (`.env`,
  `frontend/.env.local`, two throwaway test env files) appear neither in git history nor in
  tracked files.
- No `.env`, dump, key, model, `node_modules` or `.next` file was ever committed; `.env` and
  `frontend/.env.local` are ignored; build contexts exclude env files, dumps, storage and
  caches (pytest `test_deploy_config.py`).
- Client bundles: 29 static files, none containing `DOCMIND_API_URL`, `DOCMIND_BFF`,
  `refresh_token` or `docmind_refresh`; no source maps.

## Reviewed without findings

- BFF: refresh token only in the `HttpOnly; SameSite=Strict; Secure; Path=/bff/auth` cookie,
  never in JSON, storage, URLs or logs (`document.cookie` empty in Phase 10); single-flight
  and cross-tab locked refresh; one retry on 401; proxy allowlist (token endpoints, other
  paths, `%2F..` and other methods refused: 404/405); upstream redirects not followed and
  `Location` not passed through; only `Authorization`, `Content-Type`, `X-Request-ID` forwarded;
  nonce CSP on every script; no `dangerouslySetInnerHTML`, `eval` or storage use.
- API: ownership isolation (two-user checks in the release check), refresh rotation and
  reuse revocation, logout revocation, JWT algorithm and claim validation, prod guards
  (docs off: `/docs`, `/openapi.json`, `/redoc` 404 in prod; foreign Host 400; security
  headers and `Cache-Control: no-store`), upload limits enforced while streaming (also for
  chunked bodies from the BFF), filenames display-only, temporary files removed in `finally`.
- Compose: PostgreSQL has no published port in the base, smoke, release-check and
  Ollama-profile variants; every published port is on 127.0.0.1 (`api`, `web` only); `db`
  and `app` networks internal.
- CI: `pull_request` (not `pull_request_target`), `permissions: contents: read`,
  `persist-credentials: false`, actions pinned to commit SHAs, no secrets, no push, caches
  keyed on lock files.

## Commands and results (this session, local)

| Check | Command | Result |
|---|---|---|
| Backend tests (target proven: `127.0.0.1:5440/docmind_test`) | `python -m pytest -q` | 799 passed, 2 skipped (3 consecutive runs). The first full run reported 1 error not captured; the next run showed the one failure fixed in `d956435`. |
| Backend lint | `ruff check .` | clean |
| Frontend | `npm ci --ignore-scripts`; `npm audit --audit-level=low`; `npm run lint`; `npm run typecheck`; `npm test`; `npm run build` | 0 vulnerabilities; clean; clean; 43 passed; build OK |
| Python dependencies | `pip-audit --strict --disable-pip -r requirements/dev.txt`, `pip-audit --disable-pip -r requirements/runtime.txt` (2.9.0) | no known vulnerabilities; torch not auditable |
| Compose | `docker compose config` for base, smoke, release-check, `--profile ollama` | all valid; ports only `127.0.0.1` for `api` and `web` |
| Workflow | `actionlint` 1.7.7 (existing binary) | clean |
| Failure drill, before fixes | `python scripts/failure_drill.py --work-dir <tmp> --oversized-mib 640` | 13/17: F2, F3, F4 reproduced |
| Failure drill, after fixes | same, then `--skip-build` with the default 96 MiB | 16/17 (only R1), then 16/16 + 1 KNOWN |
| Release check | `python scripts/smoke_test.py --project docmind-relcheck --override compose.release-check.yaml --port 8281 --web-port 3281 --storage-dir <tmp> --env-file <tmp> --down --skip-build` | 31/31 |
| Whitespace | `git diff --check 306530c HEAD` | clean |

Every Docker run used a generated project (`docmind-drill-<hex>`, `docmind-relcheck`) with
the volume-free override and ended with `down` without `-v`; the volume list (4 volumes)
was the same before and after each run.

## Not run, and why

- Specialized secret, SAST and image scanners: not installed; not installed by this audit.
- shellcheck: not installed (the workflow's shell steps were checked by actionlint only).
- GitHub Actions: never executed on GitHub.
- Headless-browser end-to-end (Phase 10's 21 checks): not re-run; the changed code paths
  are covered by unit tests, the release check and the drill.
- TLS, reverse proxy, DNS rebinding, load tests, a real Ollama in Compose: no such setup.
- Restore into a new instance with cut-over: documented in production-deployment.md, not
  executed; only `verify` and `restore-drill` exist as evidence (Phase 11).

## Release checklist

- Local demo: tests and builds green (above), `deploy/.env` with generated secrets,
  release check 31/31, failure drill without FAIL.
- Publish source: owner reviews this report and `git log`; no `.env`, dumps or documents
  tracked (checked); optionally run gitleaks before pushing.
- Production: everything above, plus TLS reverse proxy with header overwrite (R2), a
  decision on R1 (proxy request timeout at least), off-site encrypted backups with a
  rehearsed restore-and-cut-over, monitoring/alerting on readiness and logs, a registration
  policy, and green GitHub Actions on the release commit.
