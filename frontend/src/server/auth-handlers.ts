import "server-only";

import { NextResponse, type NextRequest } from "next/server";

import type { AccessToken, TokenResponse } from "@/lib/api/types";
import {
  REFRESH_COOKIE,
  backendUrl,
  bffError,
  clearedRefreshCookie,
  copyResponseHeaders,
  refreshCookie,
  sameOriginJson,
  upstreamHeaders,
} from "./bff";

const UPSTREAM_TIMEOUT_MS = 15_000;
const MAX_AUTH_BODY_BYTES = 4096;

type Upstream = { ok: true; response: Response } | { ok: false };

async function callBackend(
  request: Request,
  path: string,
  init: { body?: unknown; authorization?: string },
): Promise<Upstream> {
  const headers = upstreamHeaders(request, { Accept: "application/json" });
  if (init.body !== undefined) headers.set("Content-Type", "application/json");
  if (init.authorization) headers.set("Authorization", init.authorization);
  try {
    const response = await fetch(backendUrl(path), {
      method: "POST",
      headers,
      body: init.body === undefined ? undefined : JSON.stringify(init.body),
      cache: "no-store",
      redirect: "manual",
      signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
    });
    return { ok: true, response };
  } catch {
    return { ok: false };
  }
}

function unavailable(): Response {
  return bffError(503, "service_unavailable", "DocMind is temporarily unavailable.");
}

function errorJson(status: number, code: string, message: string): NextResponse {
  return NextResponse.json(
    { error: { code, message, details: null } },
    { status, headers: { "Cache-Control": "no-store" } },
  );
}

/** Pass a backend error through unchanged (status, envelope, Retry-After). */
async function relayError(upstream: Response): Promise<NextResponse> {
  return new NextResponse(await upstream.text(), {
    status: upstream.status,
    headers: copyResponseHeaders(upstream.headers),
  });
}

/** Success: refresh token into the HttpOnly cookie, access token to the browser. */
function issue(tokens: TokenResponse): NextResponse {
  const body: AccessToken = { access_token: tokens.access_token, expires_in: tokens.expires_in };
  const response = NextResponse.json(body, { headers: { "Cache-Control": "no-store" } });
  response.cookies.set(refreshCookie(tokens.refresh_token, tokens.refresh_expires_in));
  return response;
}

async function readJson(request: Request): Promise<unknown> {
  const text = await request.text();
  if (text.length > MAX_AUTH_BODY_BYTES) return undefined;
  try {
    return text ? JSON.parse(text) : {};
  } catch {
    return undefined;
  }
}

function forbidden(): Response {
  return bffError(403, "forbidden", "Cross-site request rejected.");
}

export async function login(request: NextRequest): Promise<Response> {
  if (!sameOriginJson(request)) return forbidden();
  const body = await readJson(request);
  if (typeof body !== "object" || body === null) {
    return bffError(400, "bad_request", "Invalid request body.");
  }
  // Only the two fields the backend accepts; it validates them (extra fields are 422).
  const { email, password } = body as { email?: unknown; password?: unknown };
  const upstream = await callBackend(request, "/auth/login", { body: { email, password } });
  if (!upstream.ok) return unavailable();
  if (!upstream.response.ok) return relayError(upstream.response);
  return issue((await upstream.response.json()) as TokenResponse);
}

export async function refresh(request: NextRequest): Promise<Response> {
  if (!sameOriginJson(request)) return forbidden();
  const token = request.cookies.get(REFRESH_COOKIE)?.value;
  if (!token) return bffError(401, "no_session", "Not signed in.");
  const upstream = await callBackend(request, "/auth/refresh", { body: { refresh_token: token } });
  if (!upstream.ok) return unavailable();
  if (upstream.response.ok) return issue((await upstream.response.json()) as TokenResponse);
  const response = await relayError(upstream.response);
  // A rejected token is dead (expired, revoked or reused): drop it. On 429 or 5xx the
  // session may still be fine, so the cookie stays.
  if (upstream.response.status === 401) response.cookies.set(clearedRefreshCookie());
  return response;
}

/**
 * End the session on the backend, then drop the cookie. If the access token is no longer
 * accepted (expired), the refresh cookie is spent once to get a fresh one, so the session
 * is really revoked server side instead of only forgotten by this browser.
 */
export async function logout(request: NextRequest): Promise<Response> {
  if (!sameOriginJson(request)) return forbidden();
  const body = await readJson(request);
  const allSessions =
    typeof body === "object" && body !== null && (body as { all_sessions?: unknown }).all_sessions === true;
  const payload = { all_sessions: allSessions };

  let authorization = request.headers.get("authorization") ?? undefined;
  let result = authorization
    ? await callBackend(request, "/auth/logout", { body: payload, authorization })
    : null;

  const token = request.cookies.get(REFRESH_COOKIE)?.value;
  if ((!result || (result.ok && result.response.status === 401)) && token) {
    const refreshed = await callBackend(request, "/auth/refresh", { body: { refresh_token: token } });
    if (refreshed.ok && refreshed.response.ok) {
      const tokens = (await refreshed.response.json()) as TokenResponse;
      authorization = `Bearer ${tokens.access_token}`;
      result = await callBackend(request, "/auth/logout", { body: payload, authorization });
    }
  }

  const response =
    result && result.ok && result.response.status === 204
      ? new NextResponse(null, { status: 204, headers: { "Cache-Control": "no-store" } })
      : // The cookie is cleared either way; report whether the server confirmed it.
        errorJson(
          result && !result.ok ? 503 : 401,
          "logout_unconfirmed",
          "Signed out on this device; the server could not confirm the session ended.",
        );
  response.cookies.set(clearedRefreshCookie());
  return response;
}
