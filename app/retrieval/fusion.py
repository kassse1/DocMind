"""Reciprocal Rank Fusion (RRF) of the vector and full-text candidate lists.

    rrf_score(chunk) = sum over branches that returned it of 1 / (rrf_k + rank)

Only ranks are fused: cosine similarity and ts_rank live on unrelated scales, so adding
them would let whichever scale is larger dominate. A chunk found by both branches gets two
terms and usually rises to the top. rrf_k damps the head of each list: with k = 60, rank 1
scores 1/61 and rank 10 scores 1/70, so one branch's top hit cannot overwhelm agreement.

Order: rrf_score descending, then (document_id, chunk_index), the same tie-breaker as the
vector search. Equal scores are common, e.g. vector rank 1 only vs full-text rank 1 only.
"""

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from app.retrieval.full_text_search import FullTextHit
from app.retrieval.vector_search import ChunkHit


@dataclass(frozen=True)
class FusedHit:
    document_id: uuid.UUID
    filename: str
    chunk_id: uuid.UUID
    chunk_index: int
    page_number: int | None
    char_start: int
    char_end: int
    content: str
    rrf_score: float
    vector_rank: int | None
    vector_similarity: float | None
    full_text_rank: int | None
    full_text_score: float | None


def _ranked[H: (ChunkHit, FullTextHit)](hits: Iterable[H]) -> list[tuple[int, H]]:
    """1-based ranks; a repeated chunk keeps its first (best) rank."""
    seen: set[uuid.UUID] = set()
    ranked = []
    for hit in hits:
        if hit.chunk_id not in seen:
            seen.add(hit.chunk_id)
            ranked.append((len(ranked) + 1, hit))
    return ranked


def reciprocal_rank_fusion(
    vector_hits: Sequence[ChunkHit],
    full_text_hits: Sequence[FullTextHit],
    *,
    rrf_k: int,
    limit: int,
) -> list[FusedHit]:
    if rrf_k < 1 or limit < 1:
        raise ValueError("rrf_k and limit must be >= 1")
    vector = {hit.chunk_id: (rank, hit) for rank, hit in _ranked(vector_hits)}
    text = {hit.chunk_id: (rank, hit) for rank, hit in _ranked(full_text_hits)}

    fused = []
    for chunk_id in vector.keys() | text.keys():
        v_rank, v_hit = vector.get(chunk_id, (None, None))
        t_rank, t_hit = text.get(chunk_id, (None, None))
        base = v_hit or t_hit
        assert base is not None
        score = sum(1.0 / (rrf_k + rank) for rank in (v_rank, t_rank) if rank is not None)
        fused.append(
            FusedHit(
                document_id=base.document_id,
                filename=base.filename,
                chunk_id=chunk_id,
                chunk_index=base.chunk_index,
                page_number=base.page_number,
                char_start=base.char_start,
                char_end=base.char_end,
                content=base.content,
                rrf_score=score,
                vector_rank=v_rank,
                vector_similarity=v_hit.similarity if v_hit else None,
                full_text_rank=t_rank,
                full_text_score=t_hit.score if t_hit else None,
            )
        )
    fused.sort(key=lambda h: (-h.rrf_score, h.document_id, h.chunk_index))
    return fused[:limit]
