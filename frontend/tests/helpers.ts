// Test helpers. Every value here is an obvious fixture, never real user data.

import { vi } from "vitest";

export function jsonResponse(body: unknown, status = 200, headers: Record<string, string> = {}) {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

export function errorResponse(
  status: number,
  code: string,
  message: string,
  headers: Record<string, string> = {},
  details: unknown = null,
) {
  return jsonResponse({ error: { code, message, details } }, status, headers);
}

type Handler = (url: string, init: RequestInit) => Response | Promise<Response>;

/** Replace global fetch; records calls as [url, init]. */
export function mockFetch(handler: Handler) {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const fn = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = typeof input === "string" ? input : input.toString();
    calls.push({ url, init });
    return handler(url, init);
  });
  vi.stubGlobal("fetch", fn);
  return { fn, calls };
}

export function header(init: RequestInit, name: string): string | null {
  return new Headers(init.headers).get(name);
}

export const FIXTURE_TOKEN = { access_token: "fixture-access-1", expires_in: 900 };
