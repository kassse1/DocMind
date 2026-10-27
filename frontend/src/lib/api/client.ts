// The one way the UI talks to the backend: same-origin requests to the BFF proxy
// (/bff/api/* -> /api/v1/*), with the in-memory access token.

import { getAccessToken, refreshAccessToken } from "@/lib/auth/session";
import { errorFromResponse, networkError } from "./errors";
import type {
  AskRequest,
  AskResponse,
  Credentials,
  DocumentList,
  DocumentRead,
  DocumentUploadResponse,
  HybridSearchRequest,
  HybridSearchResponse,
  ReadinessResponse,
  SearchRequest,
  SearchResponse,
  SessionRead,
  UserRead,
} from "./types";

interface RequestOptions {
  method?: "GET" | "POST" | "DELETE";
  json?: unknown;
  form?: FormData;
  signal?: AbortSignal;
  /** false for public routes (register, health). */
  auth?: boolean;
}

async function send(path: string, options: RequestOptions, token: string | null) {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  let body: BodyInit | undefined;
  if (options.json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.json);
  } else if (options.form) {
    body = options.form;
  }
  try {
    return await fetch(`/bff/api${path}`, {
      method: options.method ?? "GET",
      headers,
      body,
      signal: options.signal,
      credentials: "same-origin",
      cache: "no-store",
    });
  } catch (error) {
    if (options.signal?.aborted) throw error;
    throw networkError();
  }
}

/**
 * Send a request; on 401 refresh the access token once and retry once. A failed refresh
 * ends the session (session.ts), so there is no refresh loop.
 */
export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const auth = options.auth ?? true;
  let response = await send(path, options, auth ? await getAccessToken() : null);
  if (auth && response.status === 401) {
    response = await send(path, options, await refreshAccessToken());
  }
  if (!response.ok) throw await errorFromResponse(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  readiness: (signal?: AbortSignal) =>
    apiRequest<ReadinessResponse>("/health/ready", { auth: false, signal }),
  register: (credentials: Credentials) =>
    apiRequest<UserRead>("/auth/register", { method: "POST", json: credentials, auth: false }),
  me: (signal?: AbortSignal) => apiRequest<UserRead>("/auth/me", { signal }),
  sessions: (signal?: AbortSignal) => apiRequest<SessionRead[]>("/auth/sessions", { signal }),

  listDocuments: (params: { limit?: number; offset?: number } = {}, signal?: AbortSignal) => {
    const query = new URLSearchParams();
    if (params.limit !== undefined) query.set("limit", String(params.limit));
    if (params.offset !== undefined) query.set("offset", String(params.offset));
    const suffix = query.size ? `?${query}` : "";
    return apiRequest<DocumentList>(`/documents${suffix}`, { signal });
  },
  getDocument: (id: string, signal?: AbortSignal) =>
    apiRequest<DocumentRead>(`/documents/${encodeURIComponent(id)}`, { signal }),
  deleteDocument: (id: string) =>
    apiRequest<void>(`/documents/${encodeURIComponent(id)}`, { method: "DELETE" }),
  uploadDocument: (file: File, signal?: AbortSignal) => {
    const form = new FormData();
    form.append("file", file, file.name);
    return apiRequest<DocumentUploadResponse>("/documents", { method: "POST", form, signal });
  },

  search: (body: SearchRequest, signal?: AbortSignal) =>
    apiRequest<SearchResponse>("/search", { method: "POST", json: body, signal }),
  hybridSearch: (body: HybridSearchRequest, signal?: AbortSignal) =>
    apiRequest<HybridSearchResponse>("/search/hybrid", { method: "POST", json: body, signal }),
  ask: (body: AskRequest, signal?: AbortSignal) =>
    apiRequest<AskResponse>("/ask", { method: "POST", json: body, signal }),
};
