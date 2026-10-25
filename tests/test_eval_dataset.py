import copy
import json
from collections import Counter

import pytest

from app.embeddings.fake import WordTokenizer
from app.evaluation.dataset import (
    DEFAULT_DATASET,
    LANGUAGES,
    DatasetError,
    load_dataset,
    parse_dataset,
)
from app.ingestion.chunking import Chunker


@pytest.fixture(scope="module")
def raw() -> dict:
    return json.loads(DEFAULT_DATASET.read_text(encoding="utf-8"))


def test_shipped_dataset_is_valid() -> None:
    dataset = load_dataset()
    assert dataset.version == "1.0.0"
    assert len(dataset.sha256) == 64


def test_every_language_has_at_least_ten_queries_and_all_difficulties() -> None:
    dataset = load_dataset()
    assert Counter(q.language for q in dataset.queries) == {lang: 12 for lang in LANGUAGES}
    for lang in LANGUAGES:
        difficulties = {q.difficulty for q in dataset.queries if q.language == lang}
        assert difficulties == {"straightforward", "paraphrase", "challenging"}
        # Some queries have several relevant passages (exercises Recall@k > 1 hit).
        assert any(len(q.relevant) > 1 for q in dataset.queries if q.language == lang)


def test_every_language_has_hard_negatives() -> None:
    """Most passages of each language are relevant to no query: they act as distractors."""
    dataset = load_dataset()
    judged = {p for q in dataset.queries for p in q.relevant}
    for lang in LANGUAGES:
        passages = [p for p in dataset.passages().values() if p.language == lang]
        assert len(passages) >= 12
        assert any(p.id not in judged for p in passages)


def test_passages_fit_in_one_chunk_of_default_size() -> None:
    # Word tokens here; the evaluation run re-checks with each model's real tokenizer.
    chunker = Chunker(WordTokenizer(), chunk_size=256, overlap=32)
    for passage in load_dataset().passages().values():
        [chunk] = chunker.chunk_pages([(None, passage.text)])
        assert chunk.text == passage.text


def _broken(raw: dict, mutate) -> dict:
    data = copy.deepcopy(raw)
    mutate(data)
    return data


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d["queries"].append(dict(d["queries"][0])), "Duplicate query"),
        (
            lambda d: d["documents"][0]["passages"].append(d["documents"][0]["passages"][0]),
            "Duplicate passage",
        ),
        (lambda d: d["queries"][0].update(relevant=[]), "no relevant passages"),
        (lambda d: d["queries"][0].update(relevant=["en-nope-99"]), "unknown relevant"),
        (lambda d: d["queries"][0].update(relevant=["ru-payment-01"]), "another language"),
        (
            lambda d: d["queries"][0].update(relevant=["en-billing-01", "en-billing-01"]),
            "twice",
        ),
        (lambda d: d["queries"][0].update(language="de"), "unknown language"),
        (lambda d: d["queries"][0].update(difficulty="easy"), "unknown difficulty"),
        (lambda d: d["queries"][0].update(text="  "), "no text"),
        (lambda d: d["documents"][0]["passages"][0].update(text="a  b"), "normalized"),
        (lambda d: d["documents"][0].update(passages=[]), "no passages"),
        (lambda d: d.update(queries=[]), "no queries"),
        (lambda d: d["queries"][0].pop("relevant"), "Malformed"),
    ],
)
def test_invalid_datasets_are_rejected(raw: dict, mutate, message: str) -> None:
    with pytest.raises(DatasetError, match=message):
        parse_dataset(_broken(raw, mutate))


def test_non_json_file_is_rejected(tmp_path) -> None:
    path = tmp_path / "broken.json"
    path.write_bytes(b"{not json")
    with pytest.raises(DatasetError, match="not valid UTF-8 JSON"):
        load_dataset(path)
