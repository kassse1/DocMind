"""Context selection strategies (app/rag/selection.py)."""

import uuid
from dataclasses import dataclass, replace

import pytest

from app.rag.context import build_context
from app.rag.selection import select_chunks
from app.rag.tokens import Utf8ByteCounter

DOC = uuid.uuid4()


@dataclass(frozen=True)
class Hit:
    content: str
    similarity: float
    chunk_index: int = 0
    page_number: int | None = 3
    char_start: int = 10
    document_id: uuid.UUID = DOC
    filename: str = "a.txt"

    @property
    def chunk_id(self) -> uuid.UUID:
        return uuid.uuid5(self.document_id, str(self.chunk_index))

    @property
    def char_end(self) -> int:
        return self.char_start + len(self.content)


HITS = [
    Hit("best", 0.86, 0),
    Hit("close", 0.84, 1),
    Hit("edge", 0.83, 2),
    Hit("far", 0.79, 3),
    Hit("farther", 0.70, 4),
]


def test_rank_keeps_everything_in_order() -> None:
    assert select_chunks(HITS, "rank", 0.03) == HITS


def test_relative_margin_keeps_hits_close_to_the_best_in_rank_order() -> None:
    kept = select_chunks(HITS, "relative_margin", 0.03)
    assert [h.content for h in kept] == ["best", "close", "edge"]


def test_margin_is_relative_to_the_best_hit_of_the_query() -> None:
    # Same spread, lower absolute scores: the same chunks survive. No absolute threshold.
    shifted = [replace(h, similarity=h.similarity - 0.4) for h in HITS]
    assert [h.content for h in select_chunks(shifted, "relative_margin", 0.03)] == [
        "best", "close", "edge",
    ]


def test_best_hit_always_survives() -> None:
    assert select_chunks(HITS, "relative_margin", 0.0) == [HITS[0]]
    assert select_chunks(HITS[:1], "relative_margin", 0.03) == HITS[:1]
    assert select_chunks([], "relative_margin", 0.03) == []


def test_hits_without_similarity_pass_through() -> None:
    @dataclass(frozen=True)
    class FusedLike:
        content: str
        rrf_score: float

    fused = [FusedLike("a", 0.03), FusedLike("b", 0.01)]
    assert select_chunks(fused, "relative_margin", 0.03) == fused


def test_negative_margin_is_rejected() -> None:
    with pytest.raises(ValueError):
        select_chunks(HITS, "relative_margin", -0.1)


def test_selected_chunks_keep_ids_and_offsets_through_the_context_builder() -> None:
    kept = select_chunks(HITS, "relative_margin", 0.03)
    context = build_context(kept, Utf8ByteCounter(), 10_000)
    assert [s.source_id for s in context.sources] == ["S1", "S2", "S3"]
    assert [(s.chunk_index, s.page_number, s.char_start, s.char_end) for s in context.sources] == [
        (h.chunk_index, 3, 10, 10 + len(h.content)) for h in kept
    ]
