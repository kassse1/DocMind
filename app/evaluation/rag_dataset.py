"""Labeled RAG evaluation set: questions over the search corpus plus injected documents.

Labeling rules:
- answerable: one passage of the question's language states the answer (gold); the
  expected outcome is an answer.
- unanswerable: no passage answers it; expected: abstention.
- misleading: passages on the same topic exist, but none answers the question (e.g. a
  late fee for payments when asked about late deliveries); expected: abstention.
- injection: answerable, while a passage of the same language tries to override the
  model's instructions; expected: the real answer, without the injected claim (forbidden).
- injected: the injected passages of an injection question. Whether they reach the
  context is measured, not assumed: an injection that is never retrieved tests nothing.
- answer_contains / forbidden are lexical proxies (any of the strings, case-insensitive),
  not a judgement of answer correctness.
Citation cases are scripted model replies for the validator; they are not model behavior.
"""

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from app.evaluation.dataset import (
    LANGUAGES,
    DatasetError,
    EvalDataset,
    EvalDocument,
    Passage,
    load_dataset,
    validate,
)
from app.rag.grounding import AbstentionReason

DEFAULT_RAG_DATASET = Path(__file__).parent / "datasets" / "rag_eval_v1.json"
CATEGORIES = ("answerable", "unanswerable", "misleading", "injection")
ANSWER_EXPECTED = frozenset({"answerable", "injection"})
CITATION_CASE_KINDS = (
    "valid-exact-quote",
    "duplicate-citations",
    "fabricated-source-id",
    "chunk-uuid-as-source",
    "marker-outside-context",
    "quote-not-in-source",
    "quote-from-other-source",
    "invented-number",
    "no-citations",
    "malformed-json",
    "empty-output",
    "model-abstains",
    "valid-quote-ru",
    "valid-quote-kk",
)


@dataclass(frozen=True)
class RagQuestion:
    id: str
    language: str
    category: str
    text: str
    gold: frozenset[str]
    answer_contains: tuple[str, ...]
    forbidden: tuple[str, ...]
    injected: frozenset[str]

    @property
    def expects_answer(self) -> bool:
        return self.category in ANSWER_EXPECTED


@dataclass(frozen=True)
class CitationCase:
    id: str
    question: str
    expect: str  # "answered" or an AbstentionReason value


@dataclass(frozen=True)
class RagDataset:
    name: str
    version: str
    sha256: str
    corpus: EvalDataset
    questions: tuple[RagQuestion, ...]
    citation_cases: tuple[CitationCase, ...]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DatasetError(message)


def parse_rag_dataset(raw: dict, corpus: EvalDataset, sha256: str = "") -> RagDataset:
    try:
        extra = tuple(
            EvalDocument(
                id=doc["id"],
                language=doc["language"],
                title=doc["title"],
                passages=tuple(
                    Passage(p["id"], doc["id"], doc["language"], i, p["text"])
                    for i, p in enumerate(doc["passages"])
                ),
            )
            for doc in raw.get("extra_documents", [])
        )
        questions = tuple(
            RagQuestion(
                id=q["id"],
                language=q["language"],
                category=q["category"],
                text=q["text"],
                gold=frozenset(q.get("gold", [])),
                answer_contains=tuple(q.get("answer_contains", [])),
                forbidden=tuple(q.get("forbidden", [])),
                injected=frozenset(q.get("injected", [])),
            )
            for q in raw["questions"]
        )
        cases = tuple(
            CitationCase(c["id"], c["question"], c["expect"]) for c in raw["citation_cases"]
        )
        name, version = raw["name"], raw["version"]
    except (KeyError, TypeError) as exc:
        raise DatasetError(f"Malformed RAG dataset: missing or invalid field {exc}") from None

    # Corpus + extra documents must still satisfy the corpus rules (unique, normalized).
    combined = EvalDataset(
        corpus.name, corpus.version, corpus.sha256, corpus.documents + extra, corpus.queries
    )
    validate(combined)
    passages = combined.passages()

    ids = [q.id for q in questions]
    duplicates = sorted(i for i, n in Counter(ids).items() if n > 1)
    _require(not duplicates, f"Duplicate question ids: {duplicates}")
    for q in questions:
        _require(q.language in LANGUAGES, f"Question {q.id}: unknown language")
        _require(q.category in CATEGORIES, f"Question {q.id}: unknown category")
        _require(bool(q.text.strip()), f"Question {q.id} has no text")
        if q.expects_answer:
            _require(bool(q.gold), f"Question {q.id}: an answerable question needs gold passages")
            _require(bool(q.answer_contains), f"Question {q.id}: needs answer_contains")
        else:
            _require(not q.gold, f"Question {q.id}: only answerable questions have gold passages")
        is_injection = q.category == "injection"
        _require(
            (not q.forbidden and not q.injected) or is_injection,
            f"Question {q.id}: forbidden/injected are for injection questions",
        )
        _require(
            not is_injection or (bool(q.forbidden) and bool(q.injected)),
            f"Question {q.id}: an injection question needs forbidden and injected",
        )
        bad = sorted(
            p
            for p in q.injected
            if p not in passages or p in q.gold or passages[p].language != q.language
        )
        _require(not bad, f"Question {q.id}: invalid injected passages {bad}")
        missing = sorted(q.gold - passages.keys())
        _require(not missing, f"Question {q.id}: unknown gold passages {missing}")
        # A forbidden string inside the true answer ("0 тенге" in "500 тенге") would count
        # every correct answer as a followed injection.
        clashes = [
            f for f in q.forbidden for p in q.gold if f.casefold() in passages[p].text.casefold()
        ]
        _require(not clashes, f"Question {q.id}: forbidden strings occur in gold {clashes}")
        foreign = sorted(p for p in q.gold if passages[p].language != q.language)
        _require(not foreign, f"Question {q.id}: gold passages in another language {foreign}")

    valid_outcomes = {"answered", *(r.value for r in AbstentionReason)}
    question_ids = set(ids)
    for case in cases:
        _require(case.id in CITATION_CASE_KINDS, f"Citation case {case.id}: unknown kind")
        _require(case.question in question_ids, f"Citation case {case.id}: unknown question")
        _require(case.expect in valid_outcomes, f"Citation case {case.id}: unknown outcome")
    return RagDataset(name, version, sha256, combined, questions, cases)


def load_rag_dataset(path: Path = DEFAULT_RAG_DATASET) -> RagDataset:
    data = path.read_bytes()
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DatasetError(f"RAG dataset is not valid UTF-8 JSON: {exc}") from None
    corpus_name = raw.get("corpus")
    _require(
        isinstance(corpus_name, str) and Path(corpus_name).name == corpus_name,
        "corpus must be a file name next to the RAG dataset",
    )
    corpus = load_dataset(path.parent / corpus_name)
    return parse_rag_dataset(raw, corpus, hashlib.sha256(data).hexdigest())
