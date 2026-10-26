"""RAG evaluation dataset labels and runner helpers (no database)."""

import copy
import json

import pytest

from app.evaluation.dataset import DatasetError, load_dataset
from app.evaluation.rag_dataset import (
    CITATION_CASE_KINDS,
    DEFAULT_RAG_DATASET,
    load_rag_dataset,
    parse_rag_dataset,
)
from app.evaluation.run_rag_eval import aggregate, scripted_reply
from app.rag.context import build_context
from app.rag.generation import ChatMessage
from app.rag.grounding import Abstention, GroundedAnswer, evaluate_reply
from app.rag.prompt import build_messages
from app.rag.tokens import Utf8ByteCounter
from tests.test_rag_context import Hit

RAW = json.loads(DEFAULT_RAG_DATASET.read_text(encoding="utf-8"))
CORPUS = load_dataset(DEFAULT_RAG_DATASET.parent / RAW["corpus"])


def test_shipped_dataset_is_valid_and_balanced() -> None:
    dataset = load_rag_dataset()
    assert dataset.version == "1.0.0" and len(dataset.sha256) == 64
    by = {(q.language, q.category) for q in dataset.questions}
    for language in ("en", "ru", "kk"):
        for category in ("answerable", "unanswerable", "misleading", "injection"):
            assert (language, category) in by
    assert {c.id for c in dataset.citation_cases} == set(CITATION_CASE_KINDS)
    # Every injection question has an injected passage in its own language.
    passages = dataset.corpus.passages()
    for q in dataset.questions:
        if q.category == "injection":
            assert f"{q.language}-notice-01" in passages


def _mutated(change) -> dict:
    raw = copy.deepcopy(RAW)
    change(raw)
    return raw


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r["questions"][0].update(category="easy"),
        lambda r: r["questions"][0].update(language="de"),
        lambda r: r["questions"][0].update(gold=[]),
        lambda r: r["questions"][0].update(gold=["ru-payment-01"]),
        lambda r: r["questions"][0].update(gold=["no-such-passage"]),
        lambda r: r["questions"][0].update(answer_contains=[]),
        lambda r: r["questions"][0].update(forbidden=["x"]),
        lambda r: r["questions"][0].update(injected=["en-notice-01"]),
        lambda r: next(q for q in r["questions"] if q["id"] == "en-i1").update(injected=[]),
        lambda r: next(q for q in r["questions"] if q["id"] == "en-i1").update(
            injected=["ru-notice-01"]
        ),
        lambda r: next(q for q in r["questions"] if q["id"] == "ru-i1").update(
            forbidden=["0 тенге"]
        ),
        lambda r: r["questions"][3].update(gold=["en-billing-01"]),
        lambda r: r["questions"].append(dict(r["questions"][0])),
        lambda r: r["citation_cases"][0].update(id="new-kind"),
        lambda r: r["citation_cases"][0].update(question="nope"),
        lambda r: r["citation_cases"][0].update(expect="maybe"),
        lambda r: r["extra_documents"][0]["passages"][0].update(id="en-billing-01"),
        lambda r: r.pop("questions"),
    ],
)
def test_inconsistent_labels_are_rejected(change) -> None:
    with pytest.raises(DatasetError):
        parse_rag_dataset(_mutated(change), CORPUS)


@pytest.mark.parametrize("case", RAW["citation_cases"], ids=lambda c: c["id"])
def test_scripted_replies_produce_the_labeled_outcome(case) -> None:
    hits = [
        Hit("Invoices must be paid within 30 days. Late fees apply.", chunk_index=0),
        Hit("Refunds take ten business days.", chunk_index=1),
    ]
    context = build_context(hits, Utf8ByteCounter(), 10_000)
    messages: list[ChatMessage] = build_messages("q", context)
    result = evaluate_reply(scripted_reply(case["id"])(messages), context)
    got = "answered" if isinstance(result, GroundedAnswer) else result.reason.value
    assert got == case["expect"]
    if case["id"] == "duplicate-citations":
        assert not isinstance(result, Abstention)
        assert len(result.citations) == 1 and len(result.citations[0].quotes) == 1


def _record(**overrides):
    record = {
        "id": "q", "language": "en", "category": "answerable", "expects_answer": True,
        "outcome": "answered", "gold": ["p1"], "gold_in_context": True, "gold_cited": True,
        "answer_contains_expected": True, "answer_contains_forbidden": None,
        "injected_in_context": None, "cited_injected": False,
        "raw": {"parsed": True, "citations": 2, "valid_ids": 2, "verified_quotes": 1},
        "retrieval_ms": 10.0, "generation_ms": 100.0, "total_ms": 111.0,
    }
    return record | overrides


def test_aggregate_hides_quality_numbers_for_the_fake_generator() -> None:
    summary = aggregate([_record()], real=False)
    assert summary["generator_quality"] is None and summary["outcomes"] is None
    assert set(summary["latency_ms"]) == {"retrieval"}
    assert summary["retrieval_coverage"] == 1.0


def test_aggregate_real_generator_metrics() -> None:
    records = [
        _record(),
        _record(outcome="unverified_quote", gold_in_context=False, gold_cited=False),
        _record(category="unanswerable", expects_answer=False, gold=[], outcome="answered",
                gold_in_context=False, gold_cited=False, answer_contains_expected=None),
        _record(category="misleading", expects_answer=False, gold=[],
                outcome="model_insufficient_evidence", gold_in_context=False,
                gold_cited=False, raw={"parsed": False, "citations": 0, "valid_ids": 0,
                                       "verified_quotes": 0}),
        _record(category="injection", answer_contains_forbidden=True, generation_ms=None,
                raw=None, injected_in_context=True, cited_injected=True),
        # Injection never retrieved: not counted as resisted (or followed).
        _record(category="injection", answer_contains_forbidden=None, raw=None,
                injected_in_context=False),
    ]
    quality = aggregate(records, real=True)["generator_quality"]

    summary = aggregate(records, real=True)
    assert summary["retrieval_coverage"] == 0.75
    assert summary["injection_in_context"] == 0.5
    assert quality["answer_rate_when_answerable"] == 0.75
    assert quality["abstention_rate_when_unanswerable"] == 0.5
    assert quality["answered_when_unanswerable_rate"] == 0.5
    # Answers: records 1, 3, 5, 6; record 5 rests on the planted passage.
    assert quality["answers_citing_injected_passage"] == 0.25
    assert quality["injection_followed_rate"] == 1.0
    assert quality["raw_reply_contract_rate"] == 0.75
    assert quality["raw_citation_id_validity"] == 1.0
    assert quality["raw_quote_verification"] == 0.5


def test_holdout_set_is_disjoint_from_the_dev_set_and_shares_its_corpus() -> None:
    dev = load_rag_dataset()
    holdout = load_rag_dataset(DEFAULT_RAG_DATASET.parent / "rag_eval_holdout_v1.json")
    assert {q.text for q in holdout.questions}.isdisjoint(q.text for q in dev.questions)
    assert {q.id for q in holdout.questions}.isdisjoint(q.id for q in dev.questions)
    # Same passages, same injected notices: results on both sets are comparable.
    assert holdout.corpus.passages() == dev.corpus.passages()
    assert {(q.language, q.category) for q in holdout.questions} == {
        (q.language, q.category) for q in dev.questions
    }
