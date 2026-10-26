import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.rag.grounding import AbstentionReason
from app.schemas.search import DEFAULT_TOP_K, MAX_DOCUMENT_FILTER, MAX_QUERY_CHARS, MAX_TOP_K

# semantic = the production retrieval of POST /search (default; it scored better than
# hybrid on the Phase 6 evaluation). hybrid = POST /search/hybrid with mode "hybrid".
RetrievalMode = Literal["semantic", "hybrid"]


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_QUERY_CHARS)
    ] = Field(description="Question about your documents. Treated as data, never logged.")
    # strict: "5" or true are rejected instead of coerced.
    top_k: int = Field(
        default=DEFAULT_TOP_K,
        ge=1,
        le=MAX_TOP_K,
        strict=True,
        description="Passages retrieved; the context budget may admit fewer.",
    )
    document_ids: list[uuid.UUID] | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_DOCUMENT_FILTER,
        description="Answer only from these documents of the current user.",
    )
    retrieval_mode: RetrievalMode = "semantic"

    @field_validator("document_ids")
    @classmethod
    def _deduplicate(cls, value: list[uuid.UUID] | None) -> list[uuid.UUID] | None:
        return list(dict.fromkeys(value)) if value is not None else None


class CitedQuote(BaseModel):
    # Cut from the stored passage (never copied from the model); offsets use the same
    # coordinates as the citation's char_start/char_end.
    text: str
    char_start: int
    char_end: int


class Citation(BaseModel):
    """A passage that was in the context and passed the grounding checks. Every field
    except source_id comes from the database, not from the model."""

    source_id: str = Field(description="Label of the passage in this request's prompt (S1...)")
    document_id: uuid.UUID
    filename: str
    chunk_id: uuid.UUID
    chunk_index: int
    page_number: int | None
    char_start: int
    char_end: int
    quotes: list[CitedQuote]


class Grounding(BaseModel):
    """What the server checked. A semantic entailment check is NOT performed."""

    policy: str
    citation_ids_valid: bool = Field(description="Every citation names a passage of the context")
    quotes_verified: bool = Field(description="Every quote occurs in its cited passage")
    numbers_verified: bool = Field(description="Every number in the answer is in a cited passage")
    entailment_verified: Literal[False] = Field(
        default=False,
        description="Always false: nothing proves the answer follows from the quotes.",
    )
    support_check: Literal["not_run", "model_assisted"] = Field(
        default="not_run",
        description="model_assisted: a second model pass judged the cited passages to answer "
        "the question and to state the answer (DOCMIND_RAG_GROUNDING_MODE=verified). A "
        "fallible model judgment, not a proof.",
    )
    support_check_version: str | None = None


class RetrievalInfo(BaseModel):
    mode: RetrievalMode
    top_k: int
    embedding_model: str
    retrieved_chunks: int
    # Dropped by DOCMIND_RAG_CONTEXT_SELECTION before the token budget (0 with "rank").
    excluded_by_selection: int = 0
    context_selection: str = "rank"
    context_chunks: int
    excluded_empty: int
    excluded_over_budget: int
    context_tokens: int
    context_token_budget: int
    token_counter: str


class Timing(BaseModel):
    retrieval_ms: float
    generation_ms: float | None = Field(description="null when the model was not called")
    verification_ms: float | None = Field(
        default=None, description="Support check; null unless grounding mode is verified"
    )
    total_ms: float


class AskResponse(BaseModel):
    answer: str | None = Field(
        description="Model answer that passed the grounding checks; null when abstained. "
        "Untrusted model output: render it as text."
    )
    abstained: bool
    abstention_reason: AbstentionReason | None
    citations: list[Citation]
    grounding: Grounding | None = Field(description="null when abstained")
    retrieval: RetrievalInfo
    generation_model: str | None = Field(description="null when the model was not called")
    generation_attempts: int = Field(
        default=0, description="Model calls for the answer (retries after a broken reply)"
    )
    prompt_version: str
    timing_ms: Timing
