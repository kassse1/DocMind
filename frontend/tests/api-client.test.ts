import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api/client";
import { ApiError, describeError, fieldErrors } from "@/lib/api/errors";
import { getStatus } from "@/lib/auth/session";
import { FIXTURE_TOKEN, errorResponse, header, jsonResponse, mockFetch } from "./helpers";

const ME = { id: "00000000-0000-4000-8000-000000000001", email: "fixture@example.test", created_at: "2026-01-01T00:00:00Z" };

afterEach(() => vi.unstubAllGlobals());

describe("api client", () => {
  it("refreshes once, then sends the bearer token through the BFF proxy", async () => {
    const { calls } = mockFetch((url) =>
      url === "/bff/auth/refresh" ? jsonResponse(FIXTURE_TOKEN) : jsonResponse(ME),
    );
    await expect(api.me()).resolves.toEqual(ME);
    await api.me();
    expect(calls.map((c) => c.url)).toEqual(["/bff/auth/refresh", "/bff/api/auth/me", "/bff/api/auth/me"]);
    expect(header(calls[1]!.init, "Authorization")).toBe("Bearer fixture-access-1");
    expect(getStatus()).toBe("authenticated");
  });

  it("shares one refresh between concurrent requests", async () => {
    const { calls } = mockFetch((url) =>
      url === "/bff/auth/refresh" ? jsonResponse(FIXTURE_TOKEN) : jsonResponse(ME),
    );
    await Promise.all([api.me(), api.me(), api.sessions()]);
    expect(calls.filter((c) => c.url === "/bff/auth/refresh")).toHaveLength(1);
  });

  it("holds a Web Lock while refreshing, so tabs never present the same cookie at once", async () => {
    const held: string[] = [];
    let active = false;
    const request = vi.fn(async (name: string, callback: () => Promise<unknown>) => {
      held.push(name);
      active = true;
      try {
        return await callback();
      } finally {
        active = false;
      }
    });
    vi.stubGlobal("navigator", { ...navigator, locks: { request } });
    let refreshedUnderLock = false;
    mockFetch((url) => {
      if (url === "/bff/auth/refresh") {
        refreshedUnderLock = active;
        return jsonResponse(FIXTURE_TOKEN);
      }
      return jsonResponse(ME);
    });
    await api.me();
    expect(held).toEqual(["docmind-refresh"]);
    expect(refreshedUnderLock).toBe(true);
  });

  it("retries a 401 once after refreshing, and stops when the retry fails too", async () => {
    let refreshes = 0;
    const { calls } = mockFetch((url) => {
      if (url === "/bff/auth/refresh") {
        refreshes += 1;
        return jsonResponse({ ...FIXTURE_TOKEN, access_token: `fixture-access-${refreshes}` });
      }
      return errorResponse(401, "invalid_token", "The access token is invalid or expired.");
    });
    await expect(api.me()).rejects.toMatchObject({ status: 401, code: "invalid_token" });
    // initial refresh, request, refresh after 401, one retry: no loop.
    expect(calls.map((c) => c.url)).toEqual([
      "/bff/auth/refresh",
      "/bff/api/auth/me",
      "/bff/auth/refresh",
      "/bff/api/auth/me",
    ]);
  });

  it("ends the session when the refresh token is rejected", async () => {
    mockFetch(() => errorResponse(401, "invalid_refresh_token", "Refresh token is invalid."));
    await expect(api.me()).rejects.toBeInstanceOf(ApiError);
    expect(getStatus()).toBe("anonymous");
  });

  it("keeps the session state on a rate-limited refresh and reports Retry-After", async () => {
    mockFetch(() => errorResponse(429, "rate_limited", "Too many requests.", { "Retry-After": "42" }));
    const error = await api.me().catch((e: unknown) => e);
    expect(error).toMatchObject({ status: 429, retryAfter: 42 });
    expect(describeError(error)).toBe("Too many attempts. Try again in 42 seconds.");
    expect(getStatus()).toBe("unknown");
  });

  it("does not send a token to public routes", async () => {
    const { calls } = mockFetch(() => jsonResponse(ME, 201));
    await api.register({ email: "fixture@example.test", password: "fixture-password" });
    expect(calls).toHaveLength(1);
    expect(header(calls[0]!.init, "Authorization")).toBeNull();
  });

  it("turns network failures into a typed error", async () => {
    mockFetch(() => {
      throw new TypeError("fetch failed");
    });
    await expect(api.readiness()).rejects.toMatchObject({ status: 0, code: "network_error" });
  });

  it("parses non-JSON error bodies without throwing", async () => {
    mockFetch(() => new Response("<html>bad gateway</html>", { status: 502 }));
    await expect(api.readiness()).rejects.toMatchObject({ status: 502, code: "http_error" });
  });

  it("maps validation details to fields", () => {
    const error = new ApiError({
      status: 422,
      code: "validation_error",
      message: "Request validation failed",
      details: [{ loc: ["body", "password"], msg: "String should have at least 12 characters", type: "string_too_short" }],
    });
    expect(fieldErrors(error)).toEqual({ password: "String should have at least 12 characters" });
  });
});
