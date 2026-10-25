"""Ranking metrics with binary relevance.

Conventions (also in the README):
- `ranked` is the result list in the order the search returned it. Ties in similarity
  are already broken by the search itself (document_id, then chunk_index), so the
  metrics never re-sort. Repeated ids count once, at their first position.
- Recall@k    = relevant ids in the top k / number of relevant ids for the query.
- Precision@k = relevant ids in the top k / k. The denominator is always k, even when
  fewer than k results came back or the query has fewer than k relevant passages
  (then the best possible Precision@k is below 1).
- Reciprocal rank = 1 / rank of the first relevant id, 0 if none was retrieved. Only the
  retrieved list counts, so with a retrieval depth of 5 this is RR@5 and its mean MRR@5.
- Aggregation: every query has the same weight inside a group (mean over queries).
"""

from collections.abc import Hashable, Iterable, Mapping, Sequence
from statistics import fmean


def _dedupe(ranked: Iterable[Hashable]) -> list[Hashable]:
    return list(dict.fromkeys(ranked))


def _check(relevant: Iterable[Hashable], k: int | None = None) -> frozenset:
    labels = frozenset(relevant)
    if not labels:
        raise ValueError("A query needs at least one relevant id")
    if k is not None and k < 1:
        raise ValueError(f"k must be >= 1, got {k}")
    return labels


def recall_at_k(ranked: Sequence[Hashable], relevant: Iterable[Hashable], k: int) -> float:
    labels = _check(relevant, k)
    return len(labels.intersection(_dedupe(ranked)[:k])) / len(labels)


def precision_at_k(ranked: Sequence[Hashable], relevant: Iterable[Hashable], k: int) -> float:
    labels = _check(relevant, k)
    return len(labels.intersection(_dedupe(ranked)[:k])) / k


def first_relevant_rank(ranked: Sequence[Hashable], relevant: Iterable[Hashable]) -> int | None:
    labels = _check(relevant)
    for rank, item in enumerate(_dedupe(ranked), start=1):
        if item in labels:
            return rank
    return None


def reciprocal_rank(ranked: Sequence[Hashable], relevant: Iterable[Hashable]) -> float:
    rank = first_relevant_rank(ranked, relevant)
    return 0.0 if rank is None else 1.0 / rank


def query_metrics(
    ranked: Sequence[Hashable], relevant: Iterable[Hashable], ks: Sequence[int]
) -> dict[str, float]:
    """All metrics of one query, keyed like "recall@3", "precision@3", "mrr"."""
    labels = _check(relevant)
    metrics: dict[str, float] = {}
    for k in ks:
        metrics[f"recall@{k}"] = recall_at_k(ranked, labels, k)
        metrics[f"precision@{k}"] = precision_at_k(ranked, labels, k)
    metrics["mrr"] = reciprocal_rank(ranked, labels)
    return metrics


def mean_metrics(rows: Sequence[Mapping[str, float]]) -> dict[str, float]:
    """Per-key mean over queries (each query weighs the same)."""
    if not rows:
        raise ValueError("Cannot average an empty set of queries")
    keys = list(rows[0])
    if any(list(row) != keys for row in rows):
        raise ValueError("All queries must report the same metrics")
    return {key: fmean(row[key] for row in rows) for key in keys}


def aggregate(rows: Sequence[tuple[str, Mapping[str, float]]]) -> dict:
    """Overall, per-group and macro-average metrics from (group, metrics) pairs.

    - overall: mean over all queries (micro; large groups weigh more).
    - by_group: mean over the queries of each group.
    - macro: mean of the group means (each group weighs the same, whatever its size).
    With equally sized groups, overall and macro coincide.
    """
    groups: dict[str, list[Mapping[str, float]]] = {}
    for group, metrics in rows:
        groups.setdefault(group, []).append(metrics)
    by_group = {group: mean_metrics(group_rows) for group, group_rows in sorted(groups.items())}
    return {
        "overall": mean_metrics([metrics for _, metrics in rows]),
        "by_group": by_group,
        "macro": mean_metrics(list(by_group.values())),
        "query_counts": {group: len(group_rows) for group, group_rows in sorted(groups.items())},
    }
