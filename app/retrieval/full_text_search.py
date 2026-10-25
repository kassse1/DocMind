"""Full-text retrieval over chunk content with PostgreSQL text search (tsvector + GIN).

Matching: "any term". The query is parsed as plain words by plainto_tsquery (operators
typed by the user have no special meaning), once per configuration in FULL_TEXT_CONFIGS,
and the terms are OR-ed. Plain AND semantics would require every word of a question
("how many days do I have to pay ...") to appear in one chunk and found nothing for the
questions of the evaluation set. Ranking: ts_rank (term frequency); more matching terms
rank higher. The score is only comparable within one query.

A query without any lexeme (empty, punctuation only) gives an empty tsquery, which
matches nothing: the result is an empty list, never a syntax error.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import ColumnElement, Select, Text, cast, func, literal_column, select, true
from sqlalchemy.dialects.postgresql import TSQUERY
from sqlalchemy.orm import Session

from app.models import Document, DocumentChunk
from app.models.document import FULL_TEXT_CONFIGS


@dataclass(frozen=True)
class FullTextHit:
    document_id: uuid.UUID
    filename: str
    chunk_id: uuid.UUID
    chunk_index: int
    page_number: int | None
    char_start: int
    char_end: int
    content: str
    # ts_rank of the chunk for this query (> 0). Not comparable across queries or with
    # cosine similarity.
    score: float


def any_term_tsquery(query_text: str) -> ColumnElement:
    """OR of the query's lexemes under every configuration of the search vector."""
    parts = [
        # plainto_tsquery joins lexemes with " & "; lexemes are quoted, so replacing the
        # operator text cannot touch them. The ::tsquery cast re-parses without
        # normalizing again (a stem must not be stemmed twice).
        cast(
            func.replace(
                cast(
                    func.plainto_tsquery(
                        # Constant from our code, never user input.
                        literal_column(f"'{config}'::regconfig"),
                        query_text,
                    ),
                    Text,
                ),
                " & ",
                " | ",
            ),
            TSQUERY,
        )
        for config in FULL_TEXT_CONFIGS
    ]
    query = parts[0]
    for part in parts[1:]:
        query = query.op("||", return_type=TSQUERY)(part)
    return query


def build_full_text_query(
    query_text: str,
    *,
    user_id: uuid.UUID,
    embedding_model: str,
    limit: int,
    document_ids: Sequence[uuid.UUID] | None = None,
) -> Select:
    # Computed once in a one-row subquery; the query text is a bound parameter.
    tsquery = select(any_term_tsquery(query_text).label("q")).subquery("tsq")
    score = func.ts_rank(DocumentChunk.search_vector, tsquery.c.q).label("score")
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
            score,
        )
        .select_from(DocumentChunk)
        .join(Document, Document.id == DocumentChunk.document_id)
        .join(tsquery, true())
        # Same eligibility as vector search: the caller's documents, chunks of the active
        # embedding model only (so both retrieval branches see the same corpus).
        .where(Document.user_id == user_id)
        .where(DocumentChunk.embedding_model == embedding_model)
        .where(DocumentChunk.search_vector.op("@@")(tsquery.c.q))
    )
    if document_ids is not None:
        statement = statement.where(DocumentChunk.document_id.in_(document_ids))
    return statement.order_by(
        score.desc(), DocumentChunk.document_id, DocumentChunk.chunk_index
    ).limit(limit)


def full_text_search(
    session: Session,
    query_text: str,
    *,
    user_id: uuid.UUID,
    embedding_model: str,
    limit: int,
    document_ids: Sequence[uuid.UUID] | None = None,
) -> list[FullTextHit]:
    statement = build_full_text_query(
        query_text,
        user_id=user_id,
        embedding_model=embedding_model,
        limit=limit,
        document_ids=document_ids,
    )
    return [
        FullTextHit(
            document_id=row.document_id,
            filename=row.filename,
            chunk_id=row.chunk_id,
            chunk_index=row.chunk_index,
            page_number=row.page_number,
            char_start=row.char_start,
            char_end=row.char_end,
            content=row.content,
            score=float(row.score),
        )
        for row in session.execute(statement)
    ]
