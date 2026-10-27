import type { ErrorEnvelope } from "./types";

/** An error response of the API (or of the network), with the backend envelope fields. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: unknown;
  /** Seconds from Retry-After on 429, when the server sent it. */
  readonly retryAfter: number | null;
  readonly requestId: string | null;

  constructor(init: {
    status: number;
    code: string;
    message: string;
    details?: unknown;
    retryAfter?: number | null;
    requestId?: string | null;
  }) {
    super(init.message);
    this.name = "ApiError";
    this.status = init.status;
    this.code = init.code;
    this.details = init.details ?? null;
    this.retryAfter = init.retryAfter ?? null;
    this.requestId = init.requestId ?? null;
  }
}

/** status 0: the request never got an HTTP answer (offline, DNS, server down). */
export function networkError(): ApiError {
  return new ApiError({
    status: 0,
    code: "network_error",
    message: "Could not reach DocMind. Check your connection and try again.",
  });
}

function isEnvelope(value: unknown): value is ErrorEnvelope {
  if (typeof value !== "object" || value === null || !("error" in value)) return false;
  const error = (value as { error: unknown }).error;
  return (
    typeof error === "object" &&
    error !== null &&
    typeof (error as { code?: unknown }).code === "string" &&
    typeof (error as { message?: unknown }).message === "string"
  );
}

function parseRetryAfter(value: string | null): number | null {
  if (value === null) return null;
  const seconds = Number.parseInt(value, 10);
  return Number.isFinite(seconds) && seconds >= 0 ? seconds : null;
}

/** Build an ApiError from a non-2xx response; never throws. */
export async function errorFromResponse(response: Response): Promise<ApiError> {
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    // Not JSON (e.g. a proxy error page): fall back to a generic message.
  }
  const common = {
    status: response.status,
    retryAfter: parseRetryAfter(response.headers.get("Retry-After")),
    requestId: response.headers.get("X-Request-ID"),
  };
  if (isEnvelope(body)) {
    return new ApiError({ ...common, ...body.error });
  }
  return new ApiError({
    ...common,
    code: "http_error",
    message: `The server answered with status ${response.status}.`,
  });
}

/** True when the backend says the access token is missing, invalid, expired or revoked. */
export function isAuthError(error: unknown): boolean {
  return error instanceof ApiError && error.status === 401;
}

/** Message for the user. Backend messages are written for users and never contain
 * document text; unknown cases get a neutral sentence. */
export function describeError(error: unknown): string {
  if (!(error instanceof ApiError)) return "Something went wrong. Please try again.";
  switch (error.code) {
    case "rate_limited":
      return error.retryAfter
        ? `Too many attempts. Try again in ${formatWait(error.retryAfter)}.`
        : "Too many attempts. Please wait a moment and try again.";
    case "validation_error":
      return "Some fields are not valid. Check them and try again.";
    case "database_unavailable":
    case "service_unavailable":
      return "DocMind is temporarily unavailable. Please try again shortly.";
    case "embedding_unavailable":
      return "The search model is not available right now. Please try again shortly.";
    case "generation_unavailable":
      return "Question answering is not available: no language model is configured or it is not responding.";
    default:
      return error.message;
  }
}

/** Field messages of a 422 validation_error, keyed by the last element of `loc`
 * (details are `[{loc, msg, type}]`; the backend never echoes the input). */
export function fieldErrors(error: unknown): Record<string, string> {
  if (!(error instanceof ApiError) || error.code !== "validation_error") return {};
  if (!Array.isArray(error.details)) return {};
  const fields: Record<string, string> = {};
  for (const item of error.details as unknown[]) {
    if (typeof item !== "object" || item === null) continue;
    const { loc, msg } = item as { loc?: unknown; msg?: unknown };
    const field = Array.isArray(loc) ? loc[loc.length - 1] : undefined;
    if (typeof field === "string" && typeof msg === "string" && !(field in fields)) {
      fields[field] = msg.replace(/^Value error, /, "");
    }
  }
  return fields;
}

export function formatWait(seconds: number): string {
  if (seconds < 60) return `${seconds} second${seconds === 1 ? "" : "s"}`;
  const minutes = Math.ceil(seconds / 60);
  return `${minutes} minute${minutes === 1 ? "" : "s"}`;
}
