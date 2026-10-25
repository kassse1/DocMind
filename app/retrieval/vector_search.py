"""Exact nearest-neighbour search over chunk embeddings with pgvector.

Metric: cosine distance (pgvector `<=>`), reported as similarity = 1 - distance.
Stored and query vectors are both L2-normalized (app/embeddings/base.py), so cosine
similarity equals their dot product and lies in [-1, 1]; it is a ranking signal, not a
probability.

No ANN index (HNSW/IVFFlat) yet: a sequential scan is exact, and at portfolio scale
(thousands to low hundreds of thousands of chunks) it is fast enough. An approximate
index also interacts badly with the per-user and per-document filters below (pgvector
filters after the index scan, so a filtered query can return fewer than top_k rows).
Revisit with measured latency once the data grows.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.models import Document, DocumentChunk


@dataclass(frozen=True)
class ChunkHit:
    document_id: uuid.UUID
    filename: str
    chunk_id: uuid.UUID
    chunk_index: int
    page_number: int | None
    char_start: int
    char_end: int
    content: str
    similarity: float


def build_vector_search_query(
    query_vector: np.ndarray,
    *,
    user_id: uuid.UUID,
    embedding_model: str,
    top_k: int,
    document_ids: Sequence[uuid.UUID] | None = None,
    min_similarity: float | None = None,
) -> Select:
    # The vector is a bound parameter; no user input is ever formatted into the SQL text.
    distance = DocumentChunk.embedding.cosine_distance(query_vector)
    # Ordering by the label reuses the selected value instead of sending the vector twice.
    distance_column = distance.label("distance")
    statement = (
        select(
            DocumentChunk.document_id,
            Document.filename,
            DocumentChunk.id.label("chunk_id"),
            DocumentChunk.chunk_index,
            DocumentChunk.page_number,
            DocumentChunk.char_start,
            DocumentChunk.char_end,
            DocumentChunk.content,
            distance_column,
        )
        .join(Document, Document.id == DocumentChunk.document_id)
        # Ownership isolation: only the caller's documents are ever candidates.
        .where(Document.user_id == user_id)
        # Vectors of different models are not comparable, even at equal dimension.
        .where(DocumentChunk.embedding_model == embedding_model)
    )
    if document_ids is not None:
        statement = statement.where(DocumentChunk.document_id.in_(document_ids))
    if min_similarity is not None:
        statement = statement.where(distance <= 1.0 - min_similarity)
    # Tie-breaker on a unique key (document_id, chunk_index): equal scores still come
    # back in the same order on every call.
    return statement.order_by(
        distance_column, DocumentChunk.document_id, DocumentChunk.chunk_index
    ).limit(top_k)


def vector_search(
    session: Session,
    query_vector: np.ndarray,
    *,
    user_id: uuid.UUID,
    embedding_model: str,
    top_k: int,
    document_ids: Sequence[uuid.UUID] | None = None,
    min_similarity: float | None = None,
) -> list[ChunkHit]:
    statement = build_vector_search_query(
        query_vector,
        user_id=user_id,
        embedding_model=embedding_model,
        top_k=top_k,
        document_ids=document_ids,
        min_similarity=min_similarity,
    )
    return [
        ChunkHit(
            document_id=row.document_id,
            filename=row.filename,
            chunk_id=row.chunk_id,
            chunk_index=row.chunk_index,
            page_number=row.page_number,
            char_start=row.char_start,
            char_end=row.char_end,
            content=row.content,
            similarity=1.0 - row.distance,
        )
        for row in session.execute(statement)
    ]
