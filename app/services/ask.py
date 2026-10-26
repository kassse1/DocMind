"""Question answering over the caller's documents (retrieval-augmented generation).

    retrieve (production search) -> bounded context -> prompt -> model -> grounding checks

Retrieval goes through app.services.search, so /ask inherits the document filter check
(foreign and missing ids give the same 404), owner and embedding-model filtering, the
statement timeout and the embedding_unavailable / database_unavailable errors unchanged.

Outcomes:
- 200 with an answer: it passed every check of app/rag/grounding.py.
- 200 with abstained=true and a reason: nothing retrieved, nothing fit the context, or the
  model's reply could not be grounded. The model's text is then never returned.
- 503 generation_unavailable: the provider could not reply. Distinct from abstention: the
  system failed, it did not decide the documents lack the answer.
Neither the question nor any passage is logged.
"""

import logging
import time
from dataclasses import dataclass

from fastapi import status
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError
from app.embeddings.base import Embedder
from app.models import User
from app.rag.context import GenerationContext, RetrievedChunk, build_context
from app.rag.generation import GenerationUnavailableError, Generator
from app.rag.grounding import Abstention, AbstentionReason, GroundedAnswer, evaluate_reply
from app.rag.prompt import build_messages
from app.rag.tokens import TokenCounter
from app.schemas.ask import AskRequest
from app.schemas.search import HybridSearchRequest, SearchRequest
from app.services import search as search_service

logger = logging.getLogger(__name__)


def generation_unavailable(message: str = "The answer generator is unavailable.") -> AppError:
    return AppError(
        "generation_unavailable", f"{message} Try again later.", status.HTTP_503_SERVICE_UNAVAILABLE
    )


@dataclass(frozen=True)
class AskOutcome:
    result: GroundedAnswer | Abstention
    retrieved: int
    context: GenerationContext | None
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
    hits = _retrieve(session, embedder, settings, user, request)
    # Retrieval was read-only. Ending the transaction returns the connection to the pool
    # instead of holding it idle-in-transaction for the whole generation call.
    session.rollback()
    retrieval_ms = (time.monotonic() - started) * 1000

    context: GenerationContext | None = None
    generation_ms: float | None = None
    model: str | None = None
    result: GroundedAnswer | Abstention
    if not hits:
        result = Abstention(AbstentionReason.NO_RETRIEVED_CHUNKS)
    else:
        try:
            context = build_context(hits, counter, settings.rag_context_token_budget)
        except GenerationUnavailableError:
            raise generation_unavailable("The generation tokenizer is unavailable.") from None
        if not context.sources:
            result = Abstention(AbstentionReason.NO_ELIGIBLE_CONTEXT)
        else:
            generation_started = time.monotonic()
            try:
                raw = generator.generate(build_messages(request.question, context))
            except GenerationUnavailableError:
                raise generation_unavailable() from None
            generation_ms = (time.monotonic() - generation_started) * 1000
            model = generator.model_name
            result = evaluate_reply(raw, context)

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
        context=context,
        generation_model=model,
        retrieval_ms=retrieval_ms,
        generation_ms=generation_ms,
        total_ms=total_ms,
    )
