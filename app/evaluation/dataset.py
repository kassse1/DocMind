"""Labeled evaluation dataset: documents split into passages, queries with relevance labels.

Labeling rules (binary relevance):
- A passage is relevant to a query if it alone answers the question. Passages on the same
  topic that do not answer it (hard negatives, e.g. a late-fee rule for a due-date
  question) are not relevant.
- Each language has its own document collection with invented facts, so no passage is a
  translation of another. A query is judged only against passages of its language and is
  searched only within that language's documents.
- One passage is one chunk: the evaluation checks this with the model's own tokenizer, so
  passage ids are also stable chunk ids.
"""

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from app.ingestion.normalize import normalize_text

LANGUAGES = ("en", "ru", "kk")
DIFFICULTIES = ("straightforward", "paraphrase", "challenging")
DEFAULT_DATASET = Path(__file__).parent / "datasets" / "search_eval_v1.json"


class DatasetError(ValueError):
    """The dataset file is malformed or its labels are inconsistent."""


@dataclass(frozen=True)
class Passage:
    id: str
    document_id: str
    language: str
    index: int
    text: str


@dataclass(frozen=True)
class EvalDocument:
    id: str
    language: str
    title: str
    passages: tuple[Passage, ...]


@dataclass(frozen=True)
class EvalQuery:
    id: str
    language: str
    difficulty: str
    text: str
    relevant: frozenset[str]


@dataclass(frozen=True)
class EvalDataset:
    name: str
    version: str
    sha256: str
    documents: tuple[EvalDocument, ...]
    queries: tuple[EvalQuery, ...]

    def passages(self) -> dict[str, Passage]:
        return {p.id: p for doc in self.documents for p in doc.passages}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DatasetError(message)


def _unique(ids: list[str], what: str) -> None:
    duplicates = sorted(i for i, n in Counter(ids).items() if n > 1)
    _require(not duplicates, f"Duplicate {what} ids: {duplicates}")


def parse_dataset(raw: dict, sha256: str = "") -> EvalDataset:
    try:
        documents = tuple(
            EvalDocument(
                id=doc["id"],
                language=doc["language"],
                title=doc["title"],
                passages=tuple(
                    Passage(p["id"], doc["id"], doc["language"], i, p["text"])
                    for i, p in enumerate(doc["passages"])
                ),
            )
            for doc in raw["documents"]
        )
        queries = tuple(
            EvalQuery(
                id=q["id"],
                language=q["language"],
                difficulty=q["difficulty"],
                text=q["text"],
                relevant=frozenset(q["relevant"]),
            )
            for q in raw["queries"]
        )
        dataset = EvalDataset(raw["name"], raw["version"], sha256, documents, queries)
    except (KeyError, TypeError) as exc:
        raise DatasetError(f"Malformed dataset: missing or invalid field {exc}") from None
    validate(dataset, [len(q["relevant"]) for q in raw["queries"]])
    return dataset


def validate(
    dataset: EvalDataset, label_counts: list[int] | None = None, require_queries: bool = True
) -> None:
    _require(bool(dataset.documents), "Dataset has no documents")
    # A RAG set with its own corpus has documents but no search queries.
    _require(bool(dataset.queries) or not require_queries, "Dataset has no queries")
    _unique([d.id for d in dataset.documents], "document")
    _unique([p.id for d in dataset.documents for p in d.passages], "passage")
    _unique([q.id for q in dataset.queries], "query")

    for doc in dataset.documents:
        _require(doc.language in LANGUAGES, f"Document {doc.id}: unknown language")
        _require(bool(doc.passages), f"Document {doc.id} has no passages")
        for passage in doc.passages:
            _require(bool(passage.text.strip()), f"Passage {passage.id} is empty")
            # Stored chunks are normalized text; an unnormalized passage would not match.
            _require(
                normalize_text(passage.text) == passage.text,
                f"Passage {passage.id} is not in normalized form",
            )

    passages = dataset.passages()
    for i, query in enumerate(dataset.queries):
        _require(query.language in LANGUAGES, f"Query {query.id}: unknown language")
        _require(query.difficulty in DIFFICULTIES, f"Query {query.id}: unknown difficulty")
        _require(bool(query.text.strip()), f"Query {query.id} has no text")
        _require(bool(query.relevant), f"Query {query.id} has no relevant passages")
        if label_counts is not None:
            _require(
                label_counts[i] == len(query.relevant),
                f"Query {query.id} lists a relevant passage twice",
            )
        missing = sorted(query.relevant - passages.keys())
        _require(not missing, f"Query {query.id}: unknown relevant passages {missing}")
        foreign = sorted(p for p in query.relevant if passages[p].language != query.language)
        _require(not foreign, f"Query {query.id}: relevant passages in another language {foreign}")


def load_dataset(path: Path = DEFAULT_DATASET) -> EvalDataset:
    data = path.read_bytes()
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DatasetError(f"Dataset is not valid UTF-8 JSON: {exc}") from None
    return parse_dataset(raw, hashlib.sha256(data).hexdigest())
