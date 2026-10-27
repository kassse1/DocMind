import "server-only";

// Shared by the BFF route handlers (src/app/bff/**). Runs on the Next.js server only.

/** Cookie holding the refresh token: HttpOnly, never readable by page scripts. */
export const REFRESH_COOKIE = "docmind_refresh";
/** The cookie is only sent to the BFF auth handlers, not to pages or the data proxy. */
export const REFRESH_COOKIE_PATH = "/bff/auth";

export function backendUrl(path: string): string {
  const base = process.env.DOCMIND_API_URL;
  if (!base) throw new Error("DOCMIND_API_URL is not set");
  return `${base.replace(/\/+$/, "")}/api/v1${path}`;
}

export function refreshCookie(token: string, maxAgeSeconds: number) {
  return {
    name: REFRESH_COOKIE,
    value: token,
    httpOnly: true,
    // Secure everywhere but plain-HTTP local development.
    secure: process.env.NODE_ENV === "production",
    sameSite: "strict" as const,
    path: REFRESH_COOKIE_PATH,
    maxAge: Math.max(0, Math.floor(maxAgeSeconds)),
  };
}

export function clearedRefreshCookie() {
  return { ...refreshCookie("", 0), maxAge: 0 };
}

/** JSON error in the backend envelope, so the browser client parses one shape. */
export function bffError(
  status: number,
  code: string,
  message: string,
  headers?: HeadersInit,
): Response {
  return Response.json({ error: { code, message, details: null } }, { status, headers });
}

/**
 * CSRF guard for the cookie-authenticated handlers. SameSite=Strict already keeps the
 * cookie off cross-site requests; this also requires a same-origin Origin header and a
 * JSON body, which a cross-site HTML form cannot produce.
 */
export function sameOriginJson(request: Request): boolean {
  const origin = request.headers.get("origin");
  const host = request.headers.get("x-forwarded-host") ?? request.headers.get("host");
  if (!origin || !host) return false;
  let originHost: string;
  try {
    originHost = new URL(origin).host;
  } catch {
    return false;
  }
  const type = request.headers.get("content-type") ?? "";
  return originHost === host && type.split(";")[0]?.trim() === "application/json";
}

/** Headers for a backend request on behalf of the browser. The client address is only
 * forwarded when a trusted reverse proxy sets it (DOCMIND_BFF_FORWARD_CLIENT_IP). */
export function upstreamHeaders(request: Request, extra?: Record<string, string>): Headers {
  const headers = new Headers(extra);
  const requestId = request.headers.get("x-request-id");
  if (requestId) headers.set("X-Request-ID", requestId);
  if (process.env.DOCMIND_BFF_FORWARD_CLIENT_IP === "true") {
    const forwarded = request.headers.get("x-forwarded-for");
    if (forwarded) headers.set("X-Forwarded-For", forwarded);
  }
  return headers;
}

/** Response headers worth passing back to the browser. */
export const PASSTHROUGH_RESPONSE_HEADERS = [
  "content-type",
  "retry-after",
  "x-request-id",
  "www-authenticate",
] as const;

export function copyResponseHeaders(from: Headers): Headers {
  const headers = new Headers({ "Cache-Control": "no-store" });
  for (const name of PASSTHROUGH_RESPONSE_HEADERS) {
    const value = from.get(name);
    if (value) headers.set(name, value);
  }
  return headers;
}
