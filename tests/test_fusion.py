import uuid

import pytest

from app.retrieval.full_text_search import FullTextHit
from app.retrieval.fusion import reciprocal_rank_fusion
from app.retrieval.vector_search import ChunkHit

DOC_A = uuid.UUID(int=1)
DOC_B = uuid.UUID(int=2)


def _ids() -> dict[str, uuid.UUID]:
    return {name: uuid.uuid5(uuid.NAMESPACE_DNS, name) for name in "abcdefg"}


IDS = _ids()


def v(name: str, sim: float = 0.5, doc: uuid.UUID = DOC_A, index: int = 0) -> ChunkHit:
    return ChunkHit(doc, "f.txt", IDS[name], index, None, 0, 1, name, sim)


def t(name: str, score: float = 0.1, doc: uuid.UUID = DOC_A, index: int = 0) -> FullTextHit:
    return FullTextHit(doc, "f.txt", IDS[name], index, None, 0, 1, name, score)


def names(hits) -> list[str]:
    return [h.content for h in hits]


def test_chunk_in_both_branches_beats_single_branch_hits() -> None:
    fused = reciprocal_rank_fusion(
        [v("a", index=0), v("b", index=1)],
        [t("c", index=2), t("b", index=1)],
        rrf_k=60,
        limit=10,
    )
    assert names(fused)[0] == "b"
    b = fused[0]
    assert b.rrf_score == pytest.approx(1 / 62 + 1 / 62)
    assert (b.vector_rank, b.full_text_rank) == (2, 2)
    assert b.vector_similarity == 0.5 and b.full_text_score == 0.1


def test_scores_follow_the_rrf_formula_and_keep_provenance() -> None:
    fused = reciprocal_rank_fusion([v("a", 0.9)], [t("c", 0.3)], rrf_k=10, limit=10)
    by_name = {h.content: h for h in fused}
    assert by_name["a"].rrf_score == pytest.approx(1 / 11)
    assert (by_name["a"].full_text_rank, by_name["a"].full_text_score) == (None, None)
    assert (by_name["c"].vector_rank, by_name["c"].vector_similarity) == (None, None)
    assert by_name["c"].full_text_score == 0.3


def test_equal_scores_are_ordered_by_document_then_chunk_index() -> None:
    # Vector rank 1 only vs full-text rank 1 only: identical RRF scores.
    fused = reciprocal_rank_fusion(
        [v("a", doc=DOC_B, index=0)], [t("c", doc=DOC_A, index=5)], rrf_k=60, limit=10
    )
    assert fused[0].rrf_score == fused[1].rrf_score
    assert names(fused) == ["c", "a"]  # DOC_A < DOC_B
    same_doc = reciprocal_rank_fusion(
        [v("a", index=3)], [t("c", index=1)], rrf_k=60, limit=10
    )
    assert names(same_doc) == ["c", "a"]
    # Symmetric agreement (v1+t2 vs v2+t1) also ties exactly.
    sym = reciprocal_rank_fusion(
        [v("a", index=0), v("b", index=1)], [t("b", index=1), t("a", index=0)], rrf_k=60, limit=10
    )
    assert sym[0].rrf_score == sym[1].rrf_score and names(sym) == ["a", "b"]


def test_duplicates_inside_a_branch_keep_their_best_rank() -> None:
    fused = reciprocal_rank_fusion([v("a"), v("a"), v("b", index=1)], [], rrf_k=60, limit=10)
    assert names(fused) == ["a", "b"]
    assert [h.vector_rank for h in fused] == [1, 2]  # "b" is rank 2, not 3


def test_single_branch_fallbacks() -> None:
    only_vector = reciprocal_rank_fusion([v("a"), v("b", index=1)], [], rrf_k=60, limit=10)
    assert names(only_vector) == ["a", "b"]
    only_text = reciprocal_rank_fusion([], [t("c"), t("d", index=1)], rrf_k=60, limit=10)
    assert names(only_text) == ["c", "d"]
    assert reciprocal_rank_fusion([], [], rrf_k=60, limit=10) == []


def test_limit_applies_after_fusion() -> None:
    fused = reciprocal_rank_fusion(
        [v(n, index=i) for i, n in enumerate("abcd")],
        [t(n, index=i + 4) for i, n in enumerate("efg")],
        rrf_k=60,
        limit=3,
    )
    assert len(fused) == 3


@pytest.mark.parametrize(("rrf_k", "limit"), [(0, 5), (60, 0)])
def test_invalid_parameters(rrf_k: int, limit: int) -> None:
    with pytest.raises(ValueError):
        reciprocal_rank_fusion([], [], rrf_k=rrf_k, limit=limit)
