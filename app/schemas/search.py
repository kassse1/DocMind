import uuid
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

DEFAULT_TOP_K = 5
MAX_TOP_K = 20
# Generous for a question, far below the model input limit's worth of tokens in most
# languages; longer queries would be silently truncated by the model anyway.
MAX_QUERY_CHARS = 1000
MAX_DOCUMENT_FILTER = 50
# Upper bound on candidates fetched per retrieval branch in hybrid search.
MAX_CANDIDATE_POOL = 100

SearchMode = Literal["hybrid", "vector", "full_text"]


class _SearchQuery(BaseModel):
    """Fields and rules shared by every search request."""

    # Unknown fields are rejected so a typo ("topk") fails loudly instead of being ignored.
    model_config = ConfigDict(extra="forbid")

    query: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_QUERY_CHARS)
    ] = Field(description="Natural-language query. Treated as data, never as instructions.")
    top_k: int = Field(default=DEFAULT_TOP_K, ge=1, le=MAX_TOP_K)
    document_ids: list[uuid.UUID] | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_DOCUMENT_FILTER,
        description="Restrict the search to these documents of the current user.",
    )

    @field_validator("document_ids")
    @classmethod
    def _deduplicate(cls, value: list[uuid.UUID] | None) -> list[uuid.UUID] | None:
        return list(dict.fromkeys(value)) if value is not None else None


class SearchRequest(_SearchQuery):
    min_similarity: float | None = Field(
        default=None,
        ge=-1.0,
        le=1.0,
        description="Drop results below this cosine similarity. Not a probability; "
        "useful values depend on the embedding model.",
    )


class HybridSearchRequest(_SearchQuery):
    # No min_similarity: a cosine threshold has no meaning for fused ranks (rejected, 422).
    mode: SearchMode = Field(
        default="hybrid",
        description="hybrid = vector + full-text fused with RRF; vector or full_text = one "
        "branch only, ranked the same way (full_text needs no embedding model).",
    )
    candidate_pool: int | None = Field(
        default=None,
        ge=1,
        le=MAX_CANDIDATE_POOL,
        description="Candidates fetched per branch before fusion; at least top_k. "
        "Default: DOCMIND_HYBRID_CANDIDATE_POOL.",
    )

    @model_validator(mode="after")
    def _pool_covers_top_k(self) -> "HybridSearchRequest":
        if self.candidate_pool is not None and self.candidate_pool < self.top_k:
            raise ValueError("candidate_pool must be at least top_k")
        return self


class SearchResult(BaseModel):
    document_id: uuid.UUID
    # Sanitized original filename, for display only.
    filename: str
    chunk_id: uuid.UUID
    chunk_index: int
    # NULL for formats without pages (TXT, DOCX).
    page_number: int | None
    # Offsets of the passage in the normalized page (or document) text.
    char_start: int
    char_end: int
    # Untrusted document text: clients must render it as data, never execute or follow it.
    content: str
    similarity: float = Field(
        description="Cosine similarity between query and passage embeddings, in [-1, 1]. "
        "Higher is more similar. A ranking signal, not a probability."
    )


class SearchResponse(BaseModel):
    results: list[SearchResult]
    metric: Literal["cosine"] = "cosine"
    embedding_model: str


class HybridSearchResult(BaseModel):
    document_id: uuid.UUID
    filename: str
    chunk_id: uuid.UUID
    chunk_index: int
    page_number: int | None
    char_start: int
    char_end: int
    # Untrusted document text: clients must render it as data, never execute or follow it.
    content: str
    rrf_score: float = Field(
        description="Reciprocal Rank Fusion score: sum of 1 / (rrf_k + rank) over the branches "
        "that returned this chunk. Only meaningful for ordering within one response; not a "
        "probability and not a cosine similarity."
    )
    vector_rank: int | None = Field(description="1-based rank in the vector branch, if any")
    vector_similarity: float | None = Field(
        description="Cosine similarity, present only when the vector branch returned the chunk"
    )
    full_text_rank: int | None = Field(description="1-based rank in the full-text branch, if any")
    full_text_score: float | None = Field(
        description="PostgreSQL ts_rank, present only when the full-text branch returned the "
        "chunk; comparable only within one query"
    )


class HybridSearchResponse(BaseModel):
    results: list[HybridSearchResult]
    mode: SearchMode
    fusion: Literal["rrf"] = "rrf"
    rrf_k: int
    candidate_pool: int
    # Both branches only search chunks embedded by this model (same eligible corpus).
    embedding_model: str
    full_text_configs: list[str]
