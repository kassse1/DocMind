"""Evaluation runner against the real test database, with deterministic embedders."""

from collections.abc import Sequence

import numpy as np
import pytest
from sqlalchemy import Engine, func, select, text

from app.core.config import Settings
from app.embeddings.fake import FakeEmbedder
from app.evaluation import run_search_eval
from app.evaluation.dataset import load_dataset
from app.evaluation.run_search_eval import evaluate_model, evaluate_model_retrievals, run
from app.models import User

pytestmark = pytest.mark.integration

KS = [1, 3, 5]
DATASET = load_dataset()


class OracleEmbedder(FakeEmbedder):
    """One axis per passage; a query points at the axis of its first relevant passage.

    64 dimensions (not 384): also exercises the per-model vector column size.
    """

    model_name = "test/oracle"

    def __init__(self) -> None:
        super().__init__(64)
        passages = sorted(DATASET.passages().values(), key=lambda p: p.id)
        self.axis = {p.text: i for i, p in enumerate(passages)}
        self.query_axis = {
            q.text: self.axis[DATASET.passages()[min(q.relevant)].text] for q in DATASET.queries
        }

    def _onehot(self, index: int) -> np.ndarray:
        v = np.full(self.dimension, 1e-3, dtype=np.float32)
        v[index] = 1.0
        return v / np.linalg.norm(v)

    def embed_passages(self, texts: Sequence[str]) -> np.ndarray:
        return np.array([self._onehot(self.axis[t]) for t in texts]).reshape(-1, self.dimension)

    def embed_query(self, text: str) -> np.ndarray:
        return self._onehot(self.query_axis[text])


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


def _public_users(engine: Engine) -> int:
    with engine.connect() as conn:
        return conn.scalar(text("SELECT count(*) FROM public.users"))


def test_oracle_embedder_scores_perfectly_through_the_search_service(
    pg_engine: Engine, settings: Settings
) -> None:
    assert len(DATASET.passages()) <= 64
    users_before = _public_users(pg_engine)

    result = evaluate_model(pg_engine, DATASET, OracleEmbedder(), settings, KS)

    overall = result["metrics"]["overall"]
    assert overall["mrr"] == 1.0
    assert overall["recall@1"] == pytest.approx(
        np.mean([1 / len(q.relevant) for q in DATASET.queries])
    )
    assert result["query_counts"] == {"en": 12, "kk": 12, "ru": 12}
    for query in result["queries"]:
        assert query["first_relevant_rank"] == 1
        assert len(query["retrieved"]) == 5
        # Results stay inside the query's language collection (document_ids filter).
        assert {r["passage_id"][:2] for r in query["retrieved"]} == {query["language"]}
        sims = [r["similarity"] for r in query["retrieved"]]
        assert sims == sorted(sims, reverse=True)
    # Nothing is left behind: the private schema was rolled back with the transaction.
    assert _eval_schemas(pg_engine) == []
    assert _public_users(pg_engine) == users_before


def test_runs_are_deterministic(pg_engine: Engine, settings: Settings) -> None:
    embedder = FakeEmbedder(384)
    first = evaluate_model(pg_engine, DATASET, embedder, settings, KS)
    second = evaluate_model(pg_engine, DATASET, embedder, settings, KS)
    assert first["queries"] == second["queries"]
    assert first["metrics"] == second["metrics"]


def test_unavailable_model_is_reported_and_others_still_run(
    pg_engine: Engine, settings: Settings, monkeypatch
) -> None:
    def factory(settings: Settings, dimension: int):
        if settings.embedding_model == "BAAI/bge-m3":
            raise OSError("model files are not in the local cache")
        embedder = OracleEmbedder()
        embedder.load = lambda: None
        return embedder

    monkeypatch.setattr(run_search_eval, "create_embedder", factory)

    report = run(
        pg_engine,
        DATASET,
        ["intfloat/multilingual-e5-small", "BAAI/bge-m3"],
        KS,
        settings,
        offline=True,
    )

    ok, missing = report["models"]
    assert ok["status"] == "ok" and ok["metrics"]["overall"]["mrr"] == 1.0
    assert missing == {
        "model": "BAAI/bge-m3",
        "dimension": 1024,
        "query_prefix": "",
        "passage_prefix": "",
        "status": "unavailable",
        "error": "OSError: model files are not in the local cache",
        "retrieval": "semantic",
    }
    assert {r["model"] for r in report["comparison"]} == {"intfloat/multilingual-e5-small"}
    assert report["dataset"]["queries"] == 36
    assert report["environment"]["pgvector"]


def test_isolated_schema_has_its_own_tables_and_vector_size(pg_engine: Engine) -> None:
    with run_search_eval.isolated_schema(pg_engine, 1024) as conn:
        [schema] = conn.scalars(
            text("SELECT nspname FROM pg_namespace WHERE nspname LIKE 'docmind_eval_%'")
        )
        tables = set(
            conn.scalars(
                text("SELECT tablename FROM pg_tables WHERE schemaname = :s"), {"s": schema}
            )
        )
        assert {"users", "documents", "document_chunks"} <= tables
        column_type = conn.scalar(
            text(
                "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                "WHERE attrelid = to_regclass(:t) AND attname = 'embedding'"
            ),
            {"t": f"{schema}.document_chunks"},
        )
        assert column_type == "vector(1024)"
        # The production table keeps its size.
        public_type = conn.scalar(
            text(
                "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                "WHERE attrelid = 'public.document_chunks'::regclass AND attname = 'embedding'"
            )
        )
        assert public_type == "vector(384)"
        # ORM statements on this connection are routed to the private schema.
        conn.execute(
            text(f'INSERT INTO "{schema}".users (id, email) VALUES (gen_random_uuid(), :e)'),
            {"e": "x@example.com"},
        )
        assert conn.scalar(select(func.count()).select_from(User)) == 1
    assert _eval_schemas(pg_engine) == []


def test_all_retrieval_methods_share_one_corpus_and_report_separately(
    pg_engine: Engine, settings: Settings
) -> None:
    results = evaluate_model_retrievals(
        pg_engine, DATASET, OracleEmbedder(), settings, KS, ["semantic", "full_text", "hybrid"]
    )
    assert list(results) == ["semantic", "full_text", "hybrid"]
    assert results["semantic"]["metrics"]["overall"]["mrr"] == 1.0
    # Same corpus and code path as a semantic-only run (Phase 5 numbers stay reproducible).
    alone = evaluate_model(pg_engine, DATASET, OracleEmbedder(), settings, KS)
    assert alone["queries"] == results["semantic"]["queries"]

    full_text = results["full_text"]["queries"]
    retrieved = [r for q in full_text for r in q["retrieved"]]
    assert all("rrf_score" in r and r["vector_rank"] is None for r in retrieved)
    hybrid = results["hybrid"]["queries"]
    assert any(r["vector_rank"] and r["full_text_rank"] for q in hybrid for r in q["retrieved"])
    for result in results.values():
        assert result["query_counts"] == {"en": 12, "kk": 12, "ru": 12}
        for query in result["queries"]:
            assert {r["passage_id"][:2] for r in query["retrieved"]} <= {query["language"]}
    assert _eval_schemas(pg_engine) == []
