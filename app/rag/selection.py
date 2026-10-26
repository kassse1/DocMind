"""Which retrieved chunks go into the generation context.

Strategies (DOCMIND_RAG_CONTEXT_SELECTION):
- "rank" (Phase 7.1 behavior): every retrieved chunk, in rank order.
- "relative_margin": keep the chunks whose cosine similarity is within
  DOCMIND_RAG_SIMILARITY_MARGIN of the best chunk of the same query, in rank order.

Why: with qwen2.5:3b-instruct, answerable questions whose answering chunk ranked first were
abstained on when the rest of the context held passages from other documents and
languages (Phase 7.1 scenarios, Phase 7.2 challenge baseline). Dropping chunks far below the
best one removes most of that noise while keeping the evidence: on rag_eval_v1 and the
held-out set, 0.03 is the smallest margin that kept every gold passage (0.02 lost one).

What it is not: the margin is relative to the best hit of the same query, not a calibrated
relevance threshold, and it never removes the best hit, so selection alone cannot make an
answerable question unanswerable when the best hit is the evidence. It can drop evidence
that ranks well below the best hit (e.g. the second half of a two-part question); the
evaluation measures how often. It is specific to the embedding model's similarity spread
(multilingual-e5-small), so a model change needs a new measurement.

Hits without a similarity (hybrid retrieval ranks by RRF, which has no cosine scale) are
passed through unchanged: the strategy only applies to semantic retrieval.
"""

from collections.abc import Sequence
from typing import Literal

from app.rag.context import RetrievedChunk

SelectionStrategy = Literal["rank", "relative_margin"]


def select_chunks(
    hits: Sequence[RetrievedChunk],
    strategy: SelectionStrategy,
    margin: float,
) -> list[RetrievedChunk]:
    if margin < 0:
        raise ValueError("margin must be >= 0")
    if strategy == "rank" or not hits:
        return list(hits)
    similarities = [getattr(hit, "similarity", None) for hit in hits]
    if any(isinstance(s, bool) or not isinstance(s, int | float) for s in similarities):
        return list(hits)
    best = max(similarities)  # type: ignore[type-var]
    return [hit for hit, s in zip(hits, similarities, strict=True) if s >= best - margin]
