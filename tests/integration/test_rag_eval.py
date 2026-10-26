"""RAG evaluation runner against the real test database, with deterministic models."""

import json
from collections.abc import Sequence

import numpy as np
import pytest
from sqlalchemy import Engine, text

from app.core.config import Settings
from app.embeddings.fake import FakeEmbedder
from app.evaluation.rag_dataset import load_rag_dataset
from app.evaluation.run_rag_eval import evaluate, render_markdown, run
from app.rag.generation import ChatMessage, FakeGenerator
from app.rag.tokens import Utf8ByteCounter

pytestmark = pytest.mark.integration

DATASET = load_rag_dataset()


class OracleEmbedder(FakeEmbedder):
    """One axis per passage; a question points at its gold passage (axis 0 if none)."""

    model_name = "test/oracle"

    def __init__(self) -> None:
        super().__init__(64)
        passages = sorted(DATASET.corpus.passages().values(), key=lambda p: p.id)
        self.axis = {p.text: i for i, p in enumerate(passages)}
        by_id = DATASET.corpus.passages()
        self.question_axis = {
            q.text: self.axis[by_id[min(q.gold)].text] if q.gold else 0
            for q in DATASET.questions
        }

    def _onehot(self, index: int) -> np.ndarray:
        v = np.full(self.dimension, 1e-3, dtype=np.float32)
        v[index] = 1.0
        return v / np.linalg.norm(v)

    def embed_passages(self, texts: Sequence[str]) -> np.ndarray:
        return np.array([self._onehot(self.axis[t]) for t in texts]).reshape(-1, self.dimension)

    def embed_query(self, text: str) -> np.ndarray:
        return self._onehot(self.question_axis[text])


def oracle_model(messages: Sequence[ChatMessage]) -> str:
    """Scripted 'real' model: answers from S1 when the question is answerable, else
    abstains. Exercises the quality metrics; it is not a model measurement."""
    data = json.loads(messages[-1].content)
    question = next(q for q in DATASET.questions if q.text == data["question"])
    if not question.expects_answer:
        return json.dumps({"status": "insufficient_evidence", "answer": "", "citations": []})
    text_ = data["sources"][0]["text"]
    return json.dumps(
        {"status": "answered", "answer": text_, "citations": [{"source_id": "S1", "quote": text_}]}
    )


@pytest.fixture
def settings(test_database_url: str) -> Settings:
    return Settings(_env_file=None, environment="test", database_url=test_database_url)


def _eval_schemas(engine: Engine) -> list[str]:
    with engine.connect() as conn:
        return list(
            conn.scalars(
                text("SELECT nspname FROM pg_namespace WHERE nspname LIKE 'docmind_eval_%'")
            )
        )


def test_fake_generator_run_reports_pipeline_checks_only(pg_engine, settings) -> None:
    report = run(
        pg_engine, DATASET, OracleEmbedder(), FakeGenerator(), Utf8ByteCounter(), settings,
        generator_kind="fake", top_k=5, offline=True,
    )
    results = report["results"]

    assert report["generator"]["kind"] == "fake"
    assert "No real-model measurement" in report["generator"]["note"]
    assert results["overall"]["generator_quality"] is None
    assert results["overall"]["retrieval_coverage"] == 1.0
    assert results["citation_cases"]["passed"] == results["citation_cases"]["total"] == 14
    assert len(results["questions"]) == len(DATASET.questions)
    assert "Generator quality: not measured" in render_markdown(report)
    # The private schema is gone after the run.
    assert _eval_schemas(pg_engine) == []


def test_quality_metrics_with_a_scripted_model(pg_engine, settings) -> None:
    results = evaluate(
        pg_engine, DATASET, OracleEmbedder(), FakeGenerator(oracle_model), Utf8ByteCounter(),
        settings, real_generator=True,
    )
    quality = results["overall"]["generator_quality"]
    assert quality["answer_rate_when_answerable"] == 1.0
    assert quality["abstention_rate_when_unanswerable"] == 1.0
    assert quality["unsupported_answer_rate"] == 0.0
    assert quality["gold_cited_rate_when_answered"] == 1.0
    assert quality["answer_contains_expected_rate_when_answered"] == 1.0
    assert quality["injection_followed_rate"] == 0.0
    assert quality["raw_citation_id_validity"] == quality["raw_quote_verification"] == 1.0
    assert set(results["by_language"]) == {"en", "ru", "kk"}
