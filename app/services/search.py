"""Search use cases: semantic (vector) search, and hybrid search that fuses vector and
PostgreSQL full-text candidates with Reciprocal Rank Fusion.

The query is embedded with the same embedder instance and model as indexing (one per
process, created in create_app), and only chunks embedded by that model are searched.
Query text and passages are untrusted data: they are embedded and returned, never
interpreted, and never logged.
"""

import logging
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from fastapi import status
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError
from app.embeddings.base import Embedder
from app.models import Document, User
from app.models.document import FULL_TEXT_CONFIGS
from app.retrieval.full_text_search import FullTextHit, full_text_search
from app.retrieval.fusion import FusedHit, reciprocal_rank_fusion
from app.retrieval.vector_search import ChunkHit, vector_search
from app.schemas.search import HybridSearchRequest, SearchRequest

logger = logging.getLogger(__name__)


def _missing_documents(
    session: Session, user_id: uuid.UUID, document_ids: Sequence[uuid.UUID]
) -> list[uuid.UUID]:
    owned = set(
        session.scalars(
            select(Document.id).where(Document.user_id == user_id, Document.id.in_(document_ids))
        )
    )
    return [doc_id for doc_id in document_ids if doc_id not in owned]


def _embed_query(embedder: Embedder, query: str) -> np.ndarray:
    try:
        vector = embedder.embed_query(query)
        if vector.shape != (embedder.dimension,):
            raise ValueError(f"query vector has shape {vector.shape}")
    except Exception as exc:
        # Model missing, not installed, or broken output. Class name only: library
        # messages are not vetted and could echo the query.
        logger.error("Query embedding failed: %s", type(exc).__name__)
        logger.debug("Query embedding error details", exc_info=exc)
        raise AppError(
            "embedding_unavailable",
            "The embedding model is unavailable. Try again later.",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        ) from None
    return vector


def _check_document_filter(
    session: Session, user: User, document_ids: Sequence[uuid.UUID] | None
) -> None:
    if document_ids is None:
        return
    # Checked before embedding: a bad filter should not cost a model call.
    # Another user's document is reported exactly like a nonexistent one.
    missing = _missing_documents(session, user.id, document_ids)
    if missing:
        raise AppError(
            "document_not_found",
            "One or more documents were not found.",
            status.HTTP_404_NOT_FOUND,
            details={"document_ids": [str(doc_id) for doc_id in missing]},
        )


def _set_statement_timeout(session: Session, settings: Settings) -> None:
    # Transaction-local bound on each search statement, so one expensive search cannot
    # hold a connection indefinitely. Cancellation surfaces as OperationalError (-> 503).
    session.execute(
        text("SELECT set_config('statement_timeout', :t, true)"),
        {"t": str(settings.search_timeout_ms)},
    )


def search(
    session: Session,
    embedder: Embedder,
    settings: Settings,
    user: User,
    request: SearchRequest,
) -> list[ChunkHit]:
    started = time.monotonic()
    _check_document_filter(session, user, request.document_ids)
    vector = _embed_query(embedder, request.query)
    _set_statement_timeout(session, settings)
    hits = vector_search(
        session,
        vector,
        user_id=user.id,
        embedding_model=embedder.model_name,
        top_k=request.top_k,
        document_ids=request.document_ids,
        min_similarity=request.min_similarity,
    )
    logger.info(
        "Search: user=%s top_k=%d filtered=%s results=%d ms=%.1f",
        user.id,
        request.top_k,
        request.document_ids is not None,
        len(hits),
        (time.monotonic() - started) * 1000,
    )
    return hits


@dataclass(frozen=True)
class HybridOutcome:
    hits: list[FusedHit]
    candidate_pool: int
    rrf_k: int
    full_text_configs: tuple[str, ...] = FULL_TEXT_CONFIGS


def hybrid_search(
    session: Session,
    embedder: Embedder,
    settings: Settings,
    user: User,
    request: HybridSearchRequest,
) -> HybridOutcome:
    """Both branches see the same eligible corpus: the caller's documents, the optional
    document filter, and chunks of the active embedding model, all enforced in SQL.

    Error policy: no silent degradation. In "hybrid" mode an unavailable model is a 503
    (as in /search) rather than quietly returning full-text results; clients that want
    lexical results without the model ask for mode="full_text". A database failure in
    either branch fails the request (503 via the route class), never half an answer.
    """
    started = time.monotonic()
    _check_document_filter(session, user, request.document_ids)
    pool = max(request.top_k, request.candidate_pool or settings.hybrid_candidate_pool)

    vector = None
    if request.mode in ("hybrid", "vector"):
        vector = _embed_query(embedder, request.query)
    _set_statement_timeout(session, settings)

    vector_hits: list[ChunkHit] = []
    if vector is not None:
        vector_hits = vector_search(
            session,
            vector,
            user_id=user.id,
            embedding_model=embedder.model_name,
            top_k=pool,
            document_ids=request.document_ids,
        )
    text_hits: list[FullTextHit] = []
    if request.mode in ("hybrid", "full_text"):
        text_hits = full_text_search(
            session,
            request.query,
            user_id=user.id,
            embedding_model=embedder.model_name,
            limit=pool,
            document_ids=request.document_ids,
        )
    hits = reciprocal_rank_fusion(
        vector_hits, text_hits, rrf_k=settings.hybrid_rrf_k, limit=request.top_k
    )
    logger.info(
        "Hybrid search: user=%s mode=%s top_k=%d pool=%d filtered=%s vector=%d full_text=%d "
        "results=%d ms=%.1f",
        user.id,
        request.mode,
        request.top_k,
        pool,
        request.document_ids is not None,
        len(vector_hits),
        len(text_hits),
        len(hits),
        (time.monotonic() - started) * 1000,
    )
    return HybridOutcome(hits=hits, candidate_pool=pool, rrf_k=settings.hybrid_rrf_k)
