# DocMind web frontend

Next.js 16 (App Router) + React 19 + TypeScript (strict) + Tailwind CSS 4. It talks to the
FastAPI backend in the repository root; nothing here changes the backend.

Pages: sign in, registration, dashboard, documents (upload, status, details, delete),
search (semantic and hybrid), Ask (answers with citations or an explicit abstention) and
account (profile, sessions, sign out, sign out everywhere).

## How it connects to the backend

```text
browser ──same origin──► Next.js server (BFF) ──server side──► FastAPI /api/v1
  /bff/auth/login|refresh|logout   refresh token kept in an HttpOnly cookie
  /bff/api/<allowlisted route>     streamed proxy, Bearer token forwarded as is
```

- The backend returns the refresh token in JSON and sets no cookies. The BFF puts it in a
  cookie the page cannot read (`HttpOnly`, `SameSite=Strict`, `Path=/bff/auth`, `Secure`
  in production) and gives the browser only the 15-minute access token, kept in memory.
  A reload restores the session from the cookie.
- Refreshes are serialized (one per tab, and across tabs with the Web Locks API): the
  backend treats a second use of a refresh token as theft and ends the session.
- A 401 on a data call triggers one refresh and one retry; a rejected refresh signs out.
  A 429 shows the `Retry-After` wait.
- `/bff/api/*` forwards only the documented routes (`src/server/proxy-handler.ts`); the
  token endpoints are unreachable through it, so refresh tokens never pass through page
  scripts. Bodies are streamed, so the backend's upload limit applies unchanged.
- Cookie-using handlers require a same-origin `Origin` and a JSON body (CSRF).
- Pages send a per-request nonce Content-Security-Policy (`src/proxy.ts`), so every page is
  rendered dynamically. Document text and model output are rendered as text, never HTML.
- No CORS setup is needed on the backend: the browser only talks to the Next.js origin.

## Requirements

- Node.js 24 LTS (`node --version`); npm 11.
- The DocMind API running and reachable from the Next.js server.

## Configuration

Copy `.env.example` to `.env.local` (git-ignored). Server-only variables; the frontend has
no `NEXT_PUBLIC_*` variables and no secrets.

| Variable | Default | Purpose |
|---|---|---|
| `DOCMIND_API_URL` | none (required) | Backend root, e.g. `http://127.0.0.1:8001` (no `/api/v1`) |
| `DOCMIND_BFF_FORWARD_CLIENT_IP` | `false` | Forward `X-Forwarded-For` to the backend; only behind a reverse proxy that sets it |

Rate limits are per client address. Behind the BFF the backend sees the Next.js server's
address, so by default every user shares one login and registration budget. A client can
send any `X-Forwarded-For`, and Next.js passes a client-supplied one through, so the BFF
forwards it only when `DOCMIND_BFF_FORWARD_CLIENT_IP=true`. Turn that on only behind a
reverse proxy that sets the header, and list the Next.js server in the backend's
`DOCMIND_TRUSTED_PROXIES`.

## Local development (Windows PowerShell)

```powershell
# 1. backend (repository root, its own terminal): API on 8001, plus the worker
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --port 8001
# second terminal: python -m app.worker

# 2. frontend
cd frontend
npm ci --ignore-scripts
Copy-Item .env.example .env.local      # DOCMIND_API_URL=http://127.0.0.1:8001
npm run dev                            # http://localhost:3000
```

Use `http://localhost:3000`, not `127.0.0.1:3000`, so the cookie host stays the same. The
backend's `DOCMIND_ALLOWED_HOSTS`, if set, must accept the host in `DOCMIND_API_URL`. Ask
needs a generation backend configured on the API (see the root README); without one the
page explains that question answering is unavailable and search keeps working.

## Checks

```powershell
cd frontend
npm run lint        # ESLint (Next.js core-web-vitals + TypeScript rules, no raw HTML)
npm run typecheck   # tsc --noEmit, strict
npm test            # Vitest + Testing Library (fetch mocked, no backend needed)
npm run build       # production build
npm start           # serve the build on http://localhost:3000 (needs DOCMIND_API_URL)
```

Tests cover the API client (single-flight refresh, Web Locks, 401 retry without loops,
429, network and non-JSON errors), the BFF (cookie flags, CSRF rejection, rotation, cookie
clearing, logout that still revokes with an expired access token, no client-IP forwarding
by default, proxy allowlist), the auth forms, upload checks and duplicates, search score
labels, answers, abstentions and safe rendering of untrusted text. Fixture data in
`tests/` is invented and labelled as such.

Production `next start` sets the cookie `Secure`. Browsers accept that on
`http://localhost`, but any other plain-HTTP host needs HTTPS in front.

## Known limitations

- Upload progress is not shown as a percentage (fetch has no upload progress); each file
  shows uploading, then the library shows its indexing status, polled every 3 s while any
  document is queued or indexing.
- The documents API returns no total count, so lists and dashboard counts cover at most
  the 200 newest documents and say so.
- The upload size hint is the backend default (10 MiB); a server with another
  `DOCMIND_MAX_UPLOAD_BYTES` still decides, and its 413 message is shown.
- Not containerized yet; the Phase 9 Compose stack is unchanged.
