import "server-only";

import { backendUrl, bffError, copyResponseHeaders, upstreamHeaders } from "./bff";

// Backend routes the browser may reach through /bff/api. Login, refresh and logout are
// absent on purpose: they go through the cookie handlers in /bff/auth, so a refresh token
// never passes through page scripts.
const ALLOWED: ReadonlyArray<{ method: string; pattern: RegExp }> = [
  { method: "GET", pattern: /^health(\/ready)?$/ },
  { method: "POST", pattern: /^auth\/register$/ },
  { method: "GET", pattern: /^auth\/(me|sessions)$/ },
  { method: "GET", pattern: /^documents$/ },
  { method: "POST", pattern: /^documents$/ },
  { method: "GET", pattern: /^documents\/[0-9a-fA-F-]{36}$/ },
  { method: "DELETE", pattern: /^documents\/[0-9a-fA-F-]{36}$/ },
  { method: "POST", pattern: /^search(\/hybrid)?$/ },
  { method: "POST", pattern: /^ask$/ },
];

// Model calls can be slow (the Phase 7 default generation timeout is 120 s).
const UPSTREAM_TIMEOUT_MS = 180_000;

export function isAllowed(method: string, path: string): boolean {
  return ALLOWED.some((rule) => rule.method === method && rule.pattern.test(path));
}

export async function proxy(
  request: Request,
  context: { params: Promise<{ path: string[] }> },
): Promise<Response> {
  const { path: segments } = await context.params;
  const path = segments.join("/");
  if (!isAllowed(request.method, path)) {
    return bffError(404, "not_found", "Not found.");
  }
  const incoming = new URL(request.url);
  const headers = upstreamHeaders(request, { Accept: "application/json" });
  for (const name of ["authorization", "content-type"]) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  const hasBody = request.method === "POST";
  let upstream: Response;
  try {
    upstream = await fetch(`${backendUrl(`/${path}`)}${incoming.search}`, {
      method: request.method,
      headers,
      // Streamed as is: uploads are not buffered here; the backend enforces its limits.
      body: hasBody ? request.body : undefined,
      // Required by Node's fetch for a streamed request body.
      ...(hasBody ? { duplex: "half" } : {}),
      cache: "no-store",
      redirect: "manual",
      signal: AbortSignal.any([request.signal, AbortSignal.timeout(UPSTREAM_TIMEOUT_MS)]),
    } as RequestInit);
  } catch {
    if (request.signal.aborted) return new Response(null, { status: 499 });
    return bffError(503, "service_unavailable", "DocMind is temporarily unavailable.");
  }
  return new Response(upstream.status === 204 ? null : upstream.body, {
    status: upstream.status,
    headers: copyResponseHeaders(upstream.headers),
  });
}
