# Phase 10 plan: web frontend

Status: plan written after a read-only audit of commit `047c062` (branch
`phase-9-production-readiness`). Implementation happens on `phase-10-frontend`.

Stack: Next.js (App Router), TypeScript (strict), Tailwind CSS, Vitest + Testing Library.
CLAUDE.md originally named React + Vite; the owner asked for Next.js for Phase 10, and its
server runtime is what makes an HttpOnly refresh cookie possible without backend changes
(see "Auth strategy").

**Blocker found in the audit:** Node.js was not installed on the development machine. With
the owner's approval Node.js 24.22.1 LTS (npm 11.19.1) was installed with winget.

## Verified API contract (`/api/v1`)

Source: `app/api/v1/*.py`, `app/schemas/*.py`, `app/core/errors.py`. Every error body is
`{"error": {"code": string, "message": string, "details": any}}`.

| Route | Auth | Request | Success | Notable errors |
|---|---|---|---|---|
| `GET /health` | no | — | `200 {status, app, version, environment}` | — |
| `GET /health/ready` | no | — | `200 {status: "ready", checks: {database, schema}}` | `503` |
| `POST /auth/register` | no | `{email, password}` (password 12..1024 chars, not blank; extra fields rejected) | `201 UserRead {id, email, created_at}` | `409 email_already_registered`, `422 validation_error`, `429 rate_limited` |
| `POST /auth/login` | no | `{email, password}` | `200 TokenResponse {access_token, token_type: "bearer", expires_in, refresh_token, refresh_expires_in}` | `401 invalid_credentials`, `429 rate_limited` (+ `Retry-After`) |
| `POST /auth/refresh` | no | `{refresh_token}` (max 256) | `200 TokenResponse` (old token spent) | `401 invalid_refresh_token` (reuse revokes the whole session), `429` |
| `POST /auth/logout` | Bearer | optional `{all_sessions: bool}` | `204` | `401` |
| `GET /auth/sessions` | Bearer | — | `200 [{id, created_at, last_used_at, expires_at, current}]` | `401` |
| `GET /auth/me` | Bearer | — | `200 UserRead` | `401` |
| `POST /documents` | Bearer | multipart `file` | `201 {document: DocumentRead, extraction: {page_count, pages_with_text, character_count}}` | `400 empty_file`, `409 duplicate_document` (`details.document_id`), `413 file_too_large` (`details.max_upload_bytes`), `415 unsupported_file_type` / `content_type_mismatch`, `422` |
| `GET /documents?limit&offset` | Bearer | limit 1..200 (default 50), offset 0..100000 | `200 {documents: DocumentRead[], limit, offset}` (newest first, no total) | `401` |
| `GET /documents/{id}` | Bearer | — | `200 DocumentRead` | `404 document_not_found` (also for other users' ids) |
| `DELETE /documents/{id}` | Bearer | — | `204` | `404` |
| `POST /search` | Bearer | `{query (1..1000), top_k 1..20 = 5, document_ids?, min_similarity? -1..1}` | `200 {results: [{document_id, filename, chunk_id, chunk_index, page_number, char_start, char_end, content, similarity}], metric: "cosine", embedding_model}` | `404`, `422`, `503 embedding_unavailable` |
| `POST /search/hybrid` | Bearer | `{query, top_k, document_ids?, mode: hybrid\|vector\|full_text, candidate_pool? >= top_k}` | `200 {results: [... rrf_score, vector_rank, vector_similarity, full_text_rank, full_text_score], mode, fusion: "rrf", rrf_k, candidate_pool, embedding_model, full_text_configs}` | as above |
| `POST /ask` | Bearer | `{question (1..1000), top_k (strict int) = 5, document_ids?, retrieval_mode: semantic\|hybrid}` | `200 AskResponse` (below) | `404`, `422`, `503 generation_unavailable` / `embedding_unavailable` |

`DocumentRead`: `id, filename, content_type, file_size, content_hash, status
(pending|processing|ready|failed), error_message, created_at, updated_at`.

`AskResponse`: `answer | null, abstained, abstention_reason | null` (one of
`no_retrieved_chunks, no_eligible_context, model_insufficient_evidence, empty_model_output,
malformed_model_output, missing_citations, invalid_citation, unverified_quote,
unsupported_number, unsupported_claim, verification_inconclusive`), `citations: [{source_id,
document_id, filename, chunk_id, chunk_index, page_number, char_start, char_end, quotes:
[{text, char_start, char_end}]}]`, `grounding | null {policy, citation_ids_valid,
quotes_verified, numbers_verified, entailment_verified: false, support_check,
support_check_version}`, `retrieval {...}`, `generation_model, generation_attempts,
prompt_version, timing_ms {retrieval_ms, generation_ms, verification_ms, total_ms}`.

Other facts that shape the UI:

- Upload: `.pdf`, `.docx`, `.txt` only; default limit 10 MiB (`DOCMIND_MAX_UPLOAD_BYTES`,
  not exposed by any endpoint, so the UI checks 10 MiB as a hint and the server decides).
- Generic 401s: `authentication_required` (no token), `invalid_token` (bad, expired or
  revoked token; logout revokes access tokens immediately).
- The list endpoint returns no total count: the dashboard counts only what it loaded and
  says so; there are no invented statistics.
- Scores: `similarity` is cosine (a ranking signal, not a probability); `rrf_score` is only
  for ordering inside one response. The UI labels them exactly like that.
- `content`, `answer` and quotes are untrusted text: rendered as React text nodes only.
- API responses carry `Cache-Control: no-store`, a JSON-only CSP and `X-Request-ID`.
- CORS: off unless `DOCMIND_CORS_ALLOWED_ORIGINS` is set; it never allows credentials
  (cookies), by design.

## Auth strategy

The backend returns the refresh token in JSON and never sets cookies. Keeping it in browser
JavaScript (memory, `localStorage`) would either lose the session on every reload or expose a
30-day credential to any XSS. Instead the Next.js server acts as a thin
backend-for-frontend (BFF), without any backend change:

- `POST /bff/auth/login`, `/bff/auth/refresh`, `/bff/auth/logout` are Next route handlers.
  They call the FastAPI endpoints server side, put the refresh token in an **HttpOnly,
  SameSite=Strict, Path=/bff/auth** cookie (`Secure` in production, `Max-Age` =
  `refresh_expires_in`), and return only `{access_token, expires_in}` to the browser.
- The access token (15 min) lives in memory only. On page load the app calls
  `/bff/auth/refresh` once to get one; no cookie means logged out.
- Every other API call goes through `/bff/api/*`, a streaming proxy to `/api/v1/*` that
  forwards `Authorization`, `Content-Type` and the body unchanged. One origin: no CORS
  configuration and a strict CSP (`connect-src 'self'`).
- CSRF: the cookie is SameSite=Strict, scoped to `/bff/auth`, and the auth handlers also
  require a same-origin `Origin` header and a JSON content type. Data routes need the
  in-memory Bearer token, which a cross-site page cannot read.
- Refresh races: the backend revokes the whole session when a spent refresh token is
  reused, so concurrent refreshes would log the user out. One in-flight refresh promise
  per tab, serialized across tabs with the Web Locks API (`navigator.locks`), so a second
  tab refreshes with the cookie the first one already rotated. A 401 on a data call
  triggers at most one refresh and one retry; a failed refresh logs out (no loops).
- 429: the UI shows the `Retry-After` wait and does not retry automatically.
- Rate limits are per client address: behind the BFF the backend sees the Next server's
  address. Next.js keeps a client-supplied `X-Forwarded-For` as is, so forwarding it by
  default would let anyone pick their address and dodge the limits. Decision: the BFF
  forwards it only with `DOCMIND_BFF_FORWARD_CLIENT_IP=true` (behind a reverse proxy that
  sets it, with the Next server in `DOCMIND_TRUSTED_PROXIES`); otherwise all users share
  the server's budget (safe, but one abuser can exhaust it; documented).
- Data calls are proxied by a route handler, not `next.config` rewrites: rewrites are fixed
  at build time (the backend URL would be baked into the build) and cannot restrict paths.

Configuration: `DOCMIND_API_URL` (server only, e.g. `http://127.0.0.1:8001`). No secrets in
`NEXT_PUBLIC_*`; the frontend needs no public variables at all.

## Pages

- `/login`, `/register`: labelled fields, client checks mirroring the server policy
  (password 12+ characters), server errors mapped to friendly messages, pending states.
- App shell (sidebar on desktop, drawer on mobile, skip link, visible focus):
  - `/dashboard`: recent documents and counts by status from the loaded list, readiness of
    the API, shortcuts.
  - `/documents`: drag-and-drop or picker upload (type and size hints, one upload at a
    time per file, duplicate links to the existing document), list with status badges and
    polling while anything is pending or processing, delete with a confirmation dialog.
  - `/documents/[id]`: metadata, status, error message, delete.
  - `/search`: explicit Semantic / Hybrid toggle (hybrid offers `hybrid`, `vector`,
    `full_text`), top-k, optional document filter, results with filename, page, snippet and
    the score labelled as returned.
  - `/ask`: question form, answer as text, citations with quotes and links to documents,
    abstention shown with a plain-language reason, `503 generation_unavailable` explained,
    grounding checks listed with the note that they do not prove correctness.
  - `/account`: email, account creation date, active sessions, log out, log out everywhere.

## Engineering

- `frontend/` as a separate npm project; no change to the Python project.
- Types in `src/lib/api/types.ts` written from the schemas above (one definition, used by
  the client and the components). A drift check against `openapi.json` is a possible later
  improvement; it needs the backend running with docs enabled.
- `src/lib/api/client.ts`: fetch wrapper with typed `ApiError` (status, code, message,
  details, retryAfter, requestId), `AbortSignal` support, auth handling.
- No `dangerouslySetInnerHTML`, no markdown rendering of model output.
- Security headers on the Next app: CSP, `frame-ancestors 'none'`, `nosniff`, no referrer.
- Compose: not changed in this phase unless needed; the frontend runs with `npm run dev`
  against the dev API.

## Tests and acceptance

- Unit/component tests (Vitest, jsdom, Testing Library): API error parsing, refresh
  single-flight and no-loop behaviour, 401 retry, 429 message, BFF cookie flags, login form
  validation and errors, upload validation, document status badges, search mode switching
  and score labels, ask answer/abstention/error rendering, no raw HTML rendering.
- `npm run lint`, `npm run typecheck`, `npm test`, `npm run build` pass.
- Manual check against the dev API with a throwaway account (no existing user data touched).
- `git diff --check` clean, no secrets, user files (`.vscode/`,
  `docs/evaluation/rag_eval_v1.0.0_fake.json`) untouched.
