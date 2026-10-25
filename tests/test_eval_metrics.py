import pytest

from app.evaluation.metrics import (
    aggregate,
    first_relevant_rank,
    mean_metrics,
    precision_at_k,
    query_metrics,
    recall_at_k,
    reciprocal_rank,
)

KS = (1, 3, 5)


def test_perfect_ranking() -> None:
    metrics = query_metrics(["a", "x", "y", "z", "w"], {"a"}, KS)
    assert metrics == {
        "recall@1": 1.0, "precision@1": 1.0,
        "recall@3": 1.0, "precision@3": pytest.approx(1 / 3),
        "recall@5": 1.0, "precision@5": pytest.approx(1 / 5),
        "mrr": 1.0,
    }


def test_reversed_ranking_puts_relevant_last() -> None:
    ranked = ["w", "z", "y", "x", "a"]
    assert recall_at_k(ranked, {"a"}, 1) == 0.0
    assert recall_at_k(ranked, {"a"}, 3) == 0.0
    assert recall_at_k(ranked, {"a"}, 5) == 1.0
    assert reciprocal_rank(ranked, {"a"}) == pytest.approx(0.2)


def test_no_relevant_result_scores_zero() -> None:
    metrics = query_metrics(["x", "y", "z"], {"a"}, KS)
    assert set(metrics.values()) == {0.0}
    assert first_relevant_rank(["x"], {"a"}) is None


def test_multiple_relevant_items() -> None:
    ranked = ["a", "x", "b", "y", "z"]
    relevant = {"a", "b", "c"}
    assert recall_at_k(ranked, relevant, 1) == pytest.approx(1 / 3)
    assert recall_at_k(ranked, relevant, 3) == pytest.approx(2 / 3)
    assert precision_at_k(ranked, relevant, 3) == pytest.approx(2 / 3)
    assert precision_at_k(ranked, relevant, 5) == pytest.approx(2 / 5)
    # Only the first relevant item counts for the reciprocal rank.
    assert reciprocal_rank(["x", "b", "a"], relevant) == pytest.approx(0.5)


def test_fewer_results_than_k_keep_k_as_precision_denominator() -> None:
    ranked = ["a", "b"]
    assert precision_at_k(ranked, {"a", "b"}, 5) == pytest.approx(2 / 5)
    assert recall_at_k(ranked, {"a", "b"}, 5) == 1.0
    assert query_metrics([], {"a"}, KS)["recall@5"] == 0.0


def test_duplicate_ids_count_once_at_first_position() -> None:
    ranked = ["a", "a", "a", "b"]
    assert precision_at_k(ranked, {"a"}, 3) == pytest.approx(1 / 3)
    assert recall_at_k(ranked, {"a", "b"}, 2) == 1.0  # dedup: top 2 = ["a", "b"]
    assert first_relevant_rank(["x", "x", "a"], {"a"}) == 2


def test_unlabeled_ids_are_simply_not_relevant() -> None:
    assert recall_at_k(["unknown", "a"], {"a"}, 2) == 1.0


@pytest.mark.parametrize("relevant", [set(), [], ()])
def test_empty_judgments_are_rejected(relevant) -> None:
    with pytest.raises(ValueError, match="at least one relevant"):
        query_metrics(["a"], relevant, KS)


@pytest.mark.parametrize("k", [0, -1])
def test_invalid_k_is_rejected(k: int) -> None:
    with pytest.raises(ValueError, match="k must be"):
        recall_at_k(["a"], {"a"}, k)
    with pytest.raises(ValueError, match="k must be"):
        precision_at_k(["a"], {"a"}, k)


def test_mean_metrics_rejects_empty_and_mismatched_rows() -> None:
    with pytest.raises(ValueError, match="empty"):
        mean_metrics([])
    with pytest.raises(ValueError, match="same metrics"):
        mean_metrics([{"mrr": 1.0}, {"recall@1": 1.0}])


def test_aggregate_micro_vs_macro_with_unequal_groups() -> None:
    rows = [
        ("en", {"mrr": 1.0}),
        ("en", {"mrr": 1.0}),
        ("en", {"mrr": 1.0}),
        ("kk", {"mrr": 0.0}),
    ]
    result = aggregate(rows)
    assert result["by_group"] == {"en": {"mrr": 1.0}, "kk": {"mrr": 0.0}}
    assert result["overall"] == {"mrr": 0.75}  # each query weighs the same
    assert result["macro"] == {"mrr": 0.5}  # each language weighs the same
    assert result["query_counts"] == {"en": 3, "kk": 1}
