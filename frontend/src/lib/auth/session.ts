// Browser-side session state. The access token lives in this module's memory only; the
// refresh token is an HttpOnly cookie that page scripts cannot read (see src/server/bff.ts).

import { errorFromResponse, networkError } from "@/lib/api/errors";
import type { AccessToken } from "@/lib/api/types";

/** Refresh this long before the access token expires, to absorb clock skew and latency. */
const EXPIRY_MARGIN_MS = 30_000;
const LOCK_NAME = "docmind-refresh";

export type SessionStatus = "unknown" | "authenticated" | "anonymous";

let accessToken: string | null = null;
let expiresAt = 0;
let status: SessionStatus = "unknown";
let inflight: Promise<string> | null = null;
const listeners = new Set<() => void>();

function setStatus(next: SessionStatus) {
  if (status === next) return;
  status = next;
  for (const listener of listeners) listener();
}

export function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function getStatus(): SessionStatus {
  return status;
}

function store(token: AccessToken) {
  accessToken = token.access_token;
  expiresAt = Date.now() + token.expires_in * 1000;
  setStatus("authenticated");
}

/** Forget the session locally (the cookie is cleared by the BFF). */
export function clearSession() {
  accessToken = null;
  expiresAt = 0;
  setStatus("anonymous");
}

async function postJson(path: string, body: unknown, authorization?: string): Promise<Response> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (authorization) headers.Authorization = authorization;
  try {
    return await fetch(path, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
      credentials: "same-origin",
      cache: "no-store",
    });
  } catch {
    throw networkError();
  }
}

async function doRefresh(): Promise<string> {
  const response = await postJson("/bff/auth/refresh", {});
  if (!response.ok) {
    const error = await errorFromResponse(response);
    // 401: no cookie, or the token is expired, revoked or was reused. The session is over.
    if (response.status === 401) clearSession();
    throw error;
  }
  const token = (await response.json()) as AccessToken;
  store(token);
  return token.access_token;
}

async function refreshUnderLock(locks: LockManager): Promise<string> {
  // The lock is held until the refresh settles, so the next tab sends the rotated cookie.
  return await locks.request(LOCK_NAME, () => doRefresh());
}

/**
 * Get a new access token. One refresh at a time per tab (shared promise), and one at a
 * time across tabs (Web Locks): the backend treats a second use of a refresh token as
 * theft and revokes the session, so two tabs must never present the same cookie.
 */
export function refreshAccessToken(): Promise<string> {
  if (!inflight) {
    const locks = typeof navigator !== "undefined" ? navigator.locks : undefined;
    const run = locks ? refreshUnderLock(locks) : doRefresh();
    const shared = run.finally(() => {
      inflight = null;
    });
    inflight = shared;
    return shared;
  }
  return inflight;
}

/** A usable access token, refreshing first when it is missing or about to expire. */
export async function getAccessToken(): Promise<string> {
  if (accessToken && Date.now() < expiresAt - EXPIRY_MARGIN_MS) return accessToken;
  return refreshAccessToken();
}

export async function login(email: string, password: string): Promise<void> {
  const response = await postJson("/bff/auth/login", { email, password });
  if (!response.ok) throw await errorFromResponse(response);
  store((await response.json()) as AccessToken);
}

/** End this session (or all sessions) on the server. Local state is cleared either way. */
export async function logout(allSessions = false): Promise<void> {
  const authorization = accessToken ? `Bearer ${accessToken}` : undefined;
  try {
    const response = await postJson("/bff/auth/logout", { all_sessions: allSessions }, authorization);
    if (!response.ok) throw await errorFromResponse(response);
  } finally {
    clearSession();
  }
}

/** Test hook: reset module state between tests. */
export function resetForTests() {
  accessToken = null;
  expiresAt = 0;
  status = "unknown";
  inflight = null;
  listeners.clear();
}
