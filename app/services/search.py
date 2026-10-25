"""Semantic search use case: check the document filter, embed the query, rank chunks.

The query is embedded with the same embedder instance and model as indexing (one per
process, created in create_app), and only chunks embedded by that model are searched.
Query text and passages are untrusted data: they are embedded and returned, never
interpreted, and never logged.
"""

import logging
import time
import uuid
from collections.abc import Sequence

import numpy as np
from fastapi import status
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError
from app.embeddings.base import Embedder
from app.models import Document, User
from app.retrieval.vector_search import ChunkHit, vector_search
from app.schemas.search import SearchRequest

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


def search(
    session: Session,
    embedder: Embedder,
    settings: Settings,
    user: User,
    request: SearchRequest,
) -> list[ChunkHit]:
    started = time.monotonic()
    if request.document_ids is not None:
        # Checked before embedding: a bad filter should not cost a model call.
        # Another user's document is reported exactly like a nonexistent one.
        missing = _missing_documents(session, user.id, request.document_ids)
        if missing:
            raise AppError(
                "document_not_found",
                "One or more documents were not found.",
                status.HTTP_404_NOT_FOUND,
                details={"document_ids": [str(doc_id) for doc_id in missing]},
            )

    vector = _embed_query(embedder, request.query)

    # Transaction-local bound on the query, so one expensive search cannot hold a
    # connection indefinitely. Cancellation surfaces as OperationalError (-> 503).
    session.execute(
        text("SELECT set_config('statement_timeout', :t, true)"),
        {"t": str(settings.search_timeout_ms)},
    )
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
