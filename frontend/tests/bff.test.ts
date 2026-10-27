// @vitest-environment node
import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { login, logout, refresh } from "@/server/auth-handlers";
import { isAllowed, proxy } from "@/server/proxy-handler";
import { errorResponse, header, jsonResponse, mockFetch } from "./helpers";

const ORIGIN = "http://localhost:3000";
const TOKENS = {
  access_token: "fixture-access",
  token_type: "bearer",
  expires_in: 900,
  refresh_token: "fixture-refresh-1",
  refresh_expires_in: 604800,
};

function bffRequest(path: string, init: { body?: unknown; headers?: Record<string, string>; cookie?: string } = {}) {
  const headers: Record<string, string> = {
    origin: ORIGIN,
    host: "localhost:3000",
    "content-type": "application/json",
    ...init.headers,
  };
  if (init.cookie) headers.cookie = `docmind_refresh=${init.cookie}`;
  return new NextRequest(`${ORIGIN}${path}`, {
    method: "POST",
    headers,
    body: JSON.stringify(init.body ?? {}),
  });
}

beforeEach(() => {
  vi.stubEnv("DOCMIND_API_URL", "http://backend.test:8001");
  vi.stubEnv("NODE_ENV", "production");
});
afterEach(() => vi.unstubAllEnvs());

describe("BFF auth handlers", () => {
  it("login puts the refresh token in an HttpOnly cookie and returns only the access token", async () => {
    const { calls } = mockFetch(() => jsonResponse(TOKENS));
    const response = await login(bffRequest("/bff/auth/login", { body: { email: "a@example.test", password: "p" } }));
    expect(response.status).toBe(200);
    const body = await response.json();
    expect(body).toEqual({ access_token: "fixture-access", expires_in: 900 });
    expect(JSON.stringify(body)).not.toContain("fixture-refresh");
    const cookie = response.headers.get("set-cookie") ?? "";
    expect(cookie).toContain("docmind_refresh=fixture-refresh-1");
    expect(cookie).toMatch(/HttpOnly/i);
    expect(cookie).toMatch(/SameSite=strict/i);
    expect(cookie).toMatch(/Secure/i);
    expect(cookie).toContain("Path=/bff/auth");
    expect(calls[0]!.url).toBe("http://backend.test:8001/api/v1/auth/login");
    expect(JSON.parse(calls[0]!.init.body as string)).toEqual({ email: "a@example.test", password: "p" });
  });

  it("rejects cross-site and non-JSON requests before calling the backend", async () => {
    const { calls } = mockFetch(() => jsonResponse(TOKENS));
    const crossSite = await login(bffRequest("/bff/auth/login", { headers: { origin: "https://evil.example" } }));
    const form = await login(bffRequest("/bff/auth/login", { headers: { "content-type": "application/x-www-form-urlencoded" } }));
    expect([crossSite.status, form.status]).toEqual([403, 403]);
    expect(calls).toHaveLength(0);
  });

  it("relays backend errors with Retry-After and sets no cookie", async () => {
    mockFetch(() => errorResponse(429, "rate_limited", "Too many requests.", { "Retry-After": "30" }));
    const response = await login(bffRequest("/bff/auth/login", { body: { email: "a@example.test", password: "p" } }));
    expect(response.status).toBe(429);
    expect(response.headers.get("retry-after")).toBe("30");
    expect(response.headers.get("set-cookie")).toBeNull();
    expect((await response.json()).error.code).toBe("rate_limited");
  });

  it("refresh rotates the cookie", async () => {
    const { calls } = mockFetch(() => jsonResponse({ ...TOKENS, refresh_token: "fixture-refresh-2" }));
    const response = await refresh(bffRequest("/bff/auth/refresh", { cookie: "fixture-refresh-1" }));
    expect(response.status).toBe(200);
    expect(JSON.parse(calls[0]!.init.body as string)).toEqual({ refresh_token: "fixture-refresh-1" });
    expect(response.headers.get("set-cookie")).toContain("docmind_refresh=fixture-refresh-2");
  });

  it("refresh without a cookie is 401 without a backend call", async () => {
    const { calls } = mockFetch(() => jsonResponse(TOKENS));
    const response = await refresh(bffRequest("/bff/auth/refresh"));
    expect(response.status).toBe(401);
    expect(calls).toHaveLength(0);
  });

  it("clears the cookie when the backend rejects the refresh token, keeps it on 503", async () => {
    mockFetch(() => errorResponse(401, "invalid_refresh_token", "Invalid refresh token."));
    const rejected = await refresh(bffRequest("/bff/auth/refresh", { cookie: "fixture-refresh-1" }));
    expect(rejected.headers.get("set-cookie")).toMatch(/docmind_refresh=;.*Max-Age=0/i);

    mockFetch(() => {
      throw new TypeError("connect ECONNREFUSED");
    });
    const down = await refresh(bffRequest("/bff/auth/refresh", { cookie: "fixture-refresh-1" }));
    expect(down.status).toBe(503);
    expect(down.headers.get("set-cookie")).toBeNull();
  });

  it("logout revokes on the backend and clears the cookie", async () => {
    const { calls } = mockFetch(() => new Response(null, { status: 204 }));
    const response = await logout(
      bffRequest("/bff/auth/logout", {
        body: { all_sessions: true },
        headers: { authorization: "Bearer fixture-access" },
        cookie: "fixture-refresh-1",
      }),
    );
    expect(response.status).toBe(204);
    expect(calls[0]!.url).toBe("http://backend.test:8001/api/v1/auth/logout");
    expect(header(calls[0]!.init, "Authorization")).toBe("Bearer fixture-access");
    expect(JSON.parse(calls[0]!.init.body as string)).toEqual({ all_sessions: true });
    expect(response.headers.get("set-cookie")).toMatch(/Max-Age=0/i);
  });

  it("logout with an expired access token refreshes once to revoke the session", async () => {
    const { calls } = mockFetch((url, init) => {
      if (url.endsWith("/auth/refresh")) return jsonResponse(TOKENS);
      return header(init, "Authorization") === "Bearer fixture-access"
        ? new Response(null, { status: 204 })
        : errorResponse(401, "invalid_token", "The access token is invalid or expired.");
    });
    const response = await logout(
      bffRequest("/bff/auth/logout", { headers: { authorization: "Bearer stale" }, cookie: "fixture-refresh-1" }),
    );
    expect(response.status).toBe(204);
    expect(calls.map((c) => c.url.replace("http://backend.test:8001/api/v1", ""))).toEqual([
      "/auth/logout",
      "/auth/refresh",
      "/auth/logout",
    ]);
  });

  it("does not forward a client-supplied X-Forwarded-For unless configured", async () => {
    const { calls } = mockFetch(() => jsonResponse(TOKENS));
    await login(bffRequest("/bff/auth/login", { body: {}, headers: { "x-forwarded-for": "203.0.113.9" } }));
    expect(header(calls[0]!.init, "X-Forwarded-For")).toBeNull();
    vi.stubEnv("DOCMIND_BFF_FORWARD_CLIENT_IP", "true");
    await login(bffRequest("/bff/auth/login", { body: {}, headers: { "x-forwarded-for": "203.0.113.9" } }));
    expect(header(calls[1]!.init, "X-Forwarded-For")).toBe("203.0.113.9");
  });
});

describe("BFF auth body limit (Phase 12 finding)", () => {
  function streamingRequest(path: string) {
    // An endless chunked body: counts what the handler actually pulls.
    const pulled = { bytes: 0, cancelled: false };
    const chunk = new Uint8Array(1024).fill(97);
    const body = new ReadableStream<Uint8Array>({
      pull(controller) {
        pulled.bytes += chunk.byteLength;
        controller.enqueue(chunk);
      },
      cancel() {
        pulled.cancelled = true;
      },
    });
    const request = new NextRequest(`${ORIGIN}${path}`, {
      method: "POST",
      headers: { origin: ORIGIN, host: "localhost:3000", "content-type": "application/json" },
      body,
      duplex: "half",
    } as ConstructorParameters<typeof NextRequest>[1]);
    return { request, pulled };
  }

  it("refuses a declared oversized body before reading it or calling the backend", async () => {
    const { calls } = mockFetch(() => jsonResponse(TOKENS));
    const request = bffRequest("/bff/auth/login", { headers: { "content-length": "104857600" } });
    const response = await login(request);
    expect(response.status).toBe(413);
    expect((await response.json()).error.code).toBe("payload_too_large");
    expect(calls).toHaveLength(0);
  });

  it("stops reading a streamed body as soon as it crosses the limit", async () => {
    const { calls } = mockFetch(() => jsonResponse(TOKENS));
    const { request, pulled } = streamingRequest("/bff/auth/login");
    const response = await login(request);
    expect(response.status).toBe(413);
    expect(pulled.bytes).toBeLessThanOrEqual(8 * 1024);
    expect(pulled.cancelled).toBe(true);
    expect(calls).toHaveLength(0);
  });

  it("applies the same limit to logout", async () => {
    const { calls } = mockFetch(() => new Response(null, { status: 204 }));
    const { request, pulled } = streamingRequest("/bff/auth/logout");
    const response = await logout(request);
    expect(response.status).toBe(413);
    expect(pulled.bytes).toBeLessThanOrEqual(8 * 1024);
    expect(calls).toHaveLength(0);
  });

  it("still accepts a normal body and rejects invalid JSON with 400", async () => {
    mockFetch(() => jsonResponse(TOKENS));
    expect((await login(bffRequest("/bff/auth/login", { body: { email: "a@example.test", password: "p" } }))).status).toBe(200);
    const broken = new NextRequest(`${ORIGIN}/bff/auth/login`, {
      method: "POST",
      headers: { origin: ORIGIN, host: "localhost:3000", "content-type": "application/json" },
      body: "{not json",
    });
    expect((await login(broken)).status).toBe(400);
  });
});

describe("BFF data proxy", () => {
  it("allows only the documented routes and never the token endpoints", () => {
    expect(isAllowed("GET", "documents")).toBe(true);
    expect(isAllowed("DELETE", "documents/00000000-0000-4000-8000-000000000001")).toBe(true);
    expect(isAllowed("POST", "search/hybrid")).toBe(true);
    expect(isAllowed("POST", "auth/login")).toBe(false);
    expect(isAllowed("POST", "auth/refresh")).toBe(false);
    expect(isAllowed("POST", "auth/logout")).toBe(false);
    expect(isAllowed("GET", "documents/../../docs")).toBe(false);
    expect(isAllowed("DELETE", "documents")).toBe(false);
  });

  it("forwards the bearer token and query string, and returns 404 for other paths", async () => {
    const { calls } = mockFetch(() => jsonResponse({ documents: [], limit: 5, offset: 0 }));
    const request = new Request(`${ORIGIN}/bff/api/documents?limit=5`, {
      headers: { authorization: "Bearer fixture-access", cookie: "docmind_refresh=fixture-refresh-1" },
    });
    const response = await proxy(request, { params: Promise.resolve({ path: ["documents"] }) });
    expect(response.status).toBe(200);
    expect(calls[0]!.url).toBe("http://backend.test:8001/api/v1/documents?limit=5");
    expect(header(calls[0]!.init, "Authorization")).toBe("Bearer fixture-access");
    expect(header(calls[0]!.init, "Cookie")).toBeNull();

    const blocked = await proxy(new Request(`${ORIGIN}/bff/api/auth/refresh`, { method: "POST" }), {
      params: Promise.resolve({ path: ["auth", "refresh"] }),
    });
    expect(blocked.status).toBe(404);
    expect(calls).toHaveLength(1);
  });
});
