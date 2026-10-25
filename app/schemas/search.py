import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

DEFAULT_TOP_K = 5
MAX_TOP_K = 20
# Generous for a question, far below the model input limit's worth of tokens in most
# languages; longer queries would be silently truncated by the model anyway.
MAX_QUERY_CHARS = 1000
MAX_DOCUMENT_FILTER = 50


class SearchRequest(BaseModel):
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
    min_similarity: float | None = Field(
        default=None,
        ge=-1.0,
        le=1.0,
        description="Drop results below this cosine similarity. Not a probability; "
        "useful values depend on the embedding model.",
    )

    @field_validator("document_ids")
    @classmethod
    def _deduplicate(cls, value: list[uuid.UUID] | None) -> list[uuid.UUID] | None:
        return list(dict.fromkeys(value)) if value is not None else None


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
