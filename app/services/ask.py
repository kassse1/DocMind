"""Question answering over the caller's documents (retrieval-augmented generation).

    retrieve (production search) -> bounded context -> prompt -> model -> grounding checks

Retrieval goes through app.services.search, so /ask inherits the document filter check
(foreign and missing ids give the same 404), owner and embedding-model filtering, the
statement timeout and the embedding_unavailable / database_unavailable errors unchanged.

Outcomes:
- 200 with an answer: it passed every check of app/rag/grounding.py.
- 200 with abstained=true and a reason: nothing retrieved, nothing fit the context, or the
  model's reply could not be grounded. The model's text is then never returned.
- 503 generation_unavailable: the provider could not reply, or (with an exact tokenizer)
  it reports a prompt shorter than what was sent, i.e. it truncated it. Distinct from
  abstention: the system failed, it did not decide the documents lack the answer.

Context window: the passage budget is the smaller of DOCMIND_RAG_CONTEXT_TOKEN_BUDGET and
what is left of DOCMIND_GENERATION_CONTEXT_WINDOW after the reply (max output tokens), the
instructions, the question and a reserve for the chat template. A prompt larger than the
window is not rejected by Ollama: it keeps only the end of it (measured with Ollama 0.40),
so the system instructions would be lost without any error.
Neither the question nor any passage is logged.
"""

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass

from fastapi import status
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError
from app.embeddings.base import Embedder
from app.models import User
from app.rag.context import GenerationContext, RetrievedChunk, build_context
from app.rag.generation import ChatMessage, Generation, GenerationUnavailableError, Generator
from app.rag.grounding import Abstention, AbstentionReason, GroundedAnswer, evaluate_reply
from app.rag.prompt import build_messages
from app.rag.selection import select_chunks
from app.rag.tokens import TokenCounter
from app.schemas.ask import AskRequest
from app.schemas.search import HybridSearchRequest, SearchRequest
from app.services import search as search_service

logger = logging.getLogger(__name__)

# Chat-template tokens around the messages, not visible to the token counter. Qwen2.5 adds
# 13 for a system + user pair (measured against Ollama's usage.prompt_tokens); the rest is
# margin for other templates and for the separators between serialized sources.
TEMPLATE_RESERVE_TOKENS = 64
_NO_SOURCES = GenerationContext((), 1, "", 0, 0)


def generation_unavailable(message: str = "The answer generator is unavailable.") -> AppError:
    return AppError(
        "generation_unavailable", f"{message} Try again later.", status.HTTP_503_SERVICE_UNAVAILABLE
    )


@dataclass(frozen=True)
class AskOutcome:
    result: GroundedAnswer | Abstention
    retrieved: int
    # Retrieved chunks dropped by context selection before the budget was applied.
    excluded_by_selection: int
    context: GenerationContext | None
    # Passage budget actually applied (configured budget, capped by the context window).
    token_budget: int
    generation_model: str | None
    retrieval_ms: float
    generation_ms: float | None
    total_ms: float


def _retrieve(
    session: Session, embedder: Embedder, settings: Settings, user: User, request: AskRequest
) -> list[RetrievedChunk]:
    if request.retrieval_mode == "hybrid":
        hybrid = HybridSearchRequest(
            query=request.question,
            top_k=request.top_k,
            document_ids=request.document_ids,
            mode="hybrid",
        )
        return list(search_service.hybrid_search(session, embedder, settings, user, hybrid).hits)
    semantic = SearchRequest(
        query=request.question, top_k=request.top_k, document_ids=request.document_ids
    )
    return list(search_service.search(session, embedder, settings, user, semantic))


def passage_budget(settings: Settings, counter: TokenCounter, question: str) -> int:
    """Tokens available for passages; <= 0 when even an empty context does not fit."""
    messages = build_messages(question, _NO_SOURCES, settings.rag_prompt_version)
    fixed = sum(counter.count(m.content) for m in messages)
    left = (
        settings.generation_context_window
        - settings.generation_max_output_tokens
        - fixed
        - TEMPLATE_RESERVE_TOKENS
    )
    return min(settings.rag_context_token_budget, left)


def _check_not_truncated(
    counter: TokenCounter, messages: Sequence[ChatMessage], generation: Generation
) -> None:
    """With an exact tokenizer, a server-side prompt count below the tokens we sent means
    the server dropped part of the prompt; the reply must not be used. With the byte upper
    bound no such check is possible (bytes always exceed tokens)."""
    if not counter.exact or generation.prompt_tokens is None:
        return
    sent = sum(counter.count(m.content) for m in messages)
    if generation.prompt_tokens < sent:
        logger.error(
            "Generation prompt truncated by the server: sent=%d tokens, server saw=%d",
            sent,
            generation.prompt_tokens,
        )
        raise generation_unavailable("The model server truncated the prompt.")


def ask(
    session: Session,
    embedder: Embedder,
    generator: Generator,
    counter: TokenCounter,
    settings: Settings,
    user: User,
    request: AskRequest,
) -> AskOutcome:
    started = time.monotonic()
    try:
        budget = passage_budget(settings, counter, request.question)
    except GenerationUnavailableError:
        raise generation_unavailable("The generation tokenizer is unavailable.") from None
    hits = _retrieve(session, embedder, settings, user, request)
    # Retrieval was read-only. Ending the transaction returns the connection to the pool
    # instead of holding it idle-in-transaction for the whole generation call.
    session.rollback()
    retrieval_ms = (time.monotonic() - started) * 1000

    context: GenerationContext | None = None
    generation_ms: float | None = None
    model: str | None = None
    result: GroundedAnswer | Abstention
    selected: list[RetrievedChunk] = []
    if not hits:
        result = Abstention(AbstentionReason.NO_RETRIEVED_CHUNKS)
    else:
        selected = select_chunks(
            hits, settings.rag_context_selection, settings.rag_similarity_margin
        )
        if budget >= 1:
            context = build_context(selected, counter, budget)
        else:
            # Instructions + question + reply alone fill the window: nothing fits.
            context = GenerationContext((), budget, counter.name, 0, len(selected))
        if not context.sources:
            result = Abstention(AbstentionReason.NO_ELIGIBLE_CONTEXT)
        else:
            messages = build_messages(request.question, context, settings.rag_prompt_version)
            generation_started = time.monotonic()
            try:
                generation = generator.generate(messages)
            except GenerationUnavailableError:
                raise generation_unavailable() from None
            generation_ms = (time.monotonic() - generation_started) * 1000
            model = generator.model_name
            _check_not_truncated(counter, messages, generation)
            result = evaluate_reply(generation.text, context)

    total_ms = (time.monotonic() - started) * 1000
    logger.info(
        "Ask: user=%s mode=%s top_k=%d filtered=%s retrieved=%d context=%d outcome=%s "
        "retrieval_ms=%.1f generation_ms=%s total_ms=%.1f",
        user.id,
        request.retrieval_mode,
        request.top_k,
        request.document_ids is not None,
        len(hits),
        len(context.sources) if context else 0,
        result.reason if isinstance(result, Abstention) else "answered",
        retrieval_ms,
        f"{generation_ms:.1f}" if generation_ms is not None else "-",
        total_ms,
    )
    return AskOutcome(
        result=result,
        retrieved=len(hits),
        excluded_by_selection=len(hits) - len(selected) if hits else 0,
        context=context,
        token_budget=budget,
        generation_model=model,
        retrieval_ms=retrieval_ms,
        generation_ms=generation_ms,
        total_ms=total_ms,
    )
