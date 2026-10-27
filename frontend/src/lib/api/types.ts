// Types of the DocMind API (/api/v1), written from the backend schemas in app/schemas/*.py.
// This is the only definition the frontend uses; see docs/phase-10-plan.md for the routes.

/** ISO 8601 date-time string as serialized by Pydantic. */
export type DateTimeString = string;
export type UUID = string;

export interface ErrorEnvelope {
  error: { code: string; message: string; details: unknown };
}

// --- health ---
export interface HealthResponse {
  status: "ok";
  app: string;
  version: string;
  environment: string;
}

export interface ReadinessResponse {
  status: "ready";
  checks: { database: string; schema: string };
}

// --- auth ---
export interface UserRead {
  id: UUID;
  email: string;
  created_at: DateTimeString;
}

export interface Credentials {
  email: string;
  password: string;
}

/** Backend login/refresh response. Only the BFF ever sees refresh_token. */
export interface TokenResponse {
  access_token: string;
  token_type: "bearer";
  expires_in: number;
  refresh_token: string;
  refresh_expires_in: number;
}

/** What the BFF returns to the browser: the access token only. */
export interface AccessToken {
  access_token: string;
  expires_in: number;
}

export interface SessionRead {
  id: UUID;
  created_at: DateTimeString;
  last_used_at: DateTimeString | null;
  expires_at: DateTimeString;
  current: boolean;
}

// Backend password policy (app/schemas/auth.py).
export const MIN_PASSWORD_CHARS = 12;
export const MAX_PASSWORD_CHARS = 1024;
export const MAX_EMAIL_CHARS = 320;

// --- documents ---
export type DocumentStatus = "pending" | "processing" | "ready" | "failed";

export interface DocumentRead {
  id: UUID;
  filename: string;
  content_type: string;
  file_size: number;
  content_hash: string;
  status: DocumentStatus;
  error_message: string | null;
  created_at: DateTimeString;
  updated_at: DateTimeString;
}

export interface DocumentList {
  documents: DocumentRead[];
  limit: number;
  offset: number;
}

export interface ExtractionSummary {
  page_count: number | null;
  pages_with_text: number;
  character_count: number;
}

export interface DocumentUploadResponse {
  document: DocumentRead;
  extraction: ExtractionSummary;
}

// Upload rules (app/ingestion/file_types.py, DOCMIND_MAX_UPLOAD_BYTES default). The size
// is the default only: the server is the authority and answers 413 above its limit.
export const SUPPORTED_EXTENSIONS = [".pdf", ".docx", ".txt"] as const;
export const DEFAULT_MAX_UPLOAD_BYTES = 10 * 1024 * 1024;
export const MAX_DOCUMENT_PAGE = 200;

// --- search ---
export const DEFAULT_TOP_K = 5;
export const MAX_TOP_K = 20;
export const MAX_QUERY_CHARS = 1000;
export const MAX_DOCUMENT_FILTER = 50;

export type HybridMode = "hybrid" | "vector" | "full_text";

interface SearchQuery {
  query: string;
  top_k?: number;
  document_ids?: UUID[];
}

export interface SearchRequest extends SearchQuery {
  min_similarity?: number;
}

export interface HybridSearchRequest extends SearchQuery {
  mode?: HybridMode;
  candidate_pool?: number;
}

interface Passage {
  document_id: UUID;
  filename: string;
  chunk_id: UUID;
  chunk_index: number;
  page_number: number | null;
  char_start: number;
  char_end: number;
  /** Untrusted document text: render as text only. */
  content: string;
}

export interface SearchResult extends Passage {
  /** Cosine similarity in [-1, 1]; a ranking signal, not a probability. */
  similarity: number;
}

export interface SearchResponse {
  results: SearchResult[];
  metric: "cosine";
  embedding_model: string;
}

export interface HybridSearchResult extends Passage {
  /** Reciprocal Rank Fusion score; only for ordering within one response. */
  rrf_score: number;
  vector_rank: number | null;
  vector_similarity: number | null;
  full_text_rank: number | null;
  full_text_score: number | null;
}

export interface HybridSearchResponse {
  results: HybridSearchResult[];
  mode: HybridMode;
  fusion: "rrf";
  rrf_k: number;
  candidate_pool: number;
  embedding_model: string;
  full_text_configs: string[];
}

// --- ask ---
export type RetrievalMode = "semantic" | "hybrid";

export interface AskRequest {
  question: string;
  top_k?: number;
  document_ids?: UUID[];
  retrieval_mode?: RetrievalMode;
}

export type AbstentionReason =
  | "no_retrieved_chunks"
  | "no_eligible_context"
  | "model_insufficient_evidence"
  | "empty_model_output"
  | "malformed_model_output"
  | "missing_citations"
  | "invalid_citation"
  | "unverified_quote"
  | "unsupported_number"
  | "unsupported_claim"
  | "verification_inconclusive";

export interface CitedQuote {
  text: string;
  char_start: number;
  char_end: number;
}

export interface Citation {
  source_id: string;
  document_id: UUID;
  filename: string;
  chunk_id: UUID;
  chunk_index: number;
  page_number: number | null;
  char_start: number;
  char_end: number;
  quotes: CitedQuote[];
}

export interface Grounding {
  policy: string;
  citation_ids_valid: boolean;
  quotes_verified: boolean;
  numbers_verified: boolean;
  entailment_verified: false;
  support_check: "not_run" | "model_assisted";
  support_check_version: string | null;
}

export interface RetrievalInfo {
  mode: RetrievalMode;
  top_k: number;
  embedding_model: string;
  retrieved_chunks: number;
  excluded_by_selection: number;
  context_selection: string;
  context_chunks: number;
  excluded_empty: number;
  excluded_over_budget: number;
  context_tokens: number;
  context_token_budget: number;
  token_counter: string;
}

export interface Timing {
  retrieval_ms: number;
  generation_ms: number | null;
  verification_ms: number | null;
  total_ms: number;
}

export interface AskResponse {
  /** Untrusted model output: render as text only. */
  answer: string | null;
  abstained: boolean;
  abstention_reason: AbstentionReason | null;
  citations: Citation[];
  grounding: Grounding | null;
  retrieval: RetrievalInfo;
  generation_model: string | null;
  generation_attempts: number;
  prompt_version: string;
  timing_ms: Timing;
}
