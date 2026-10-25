"""POST /api/v1/search/hybrid end to end against the real test database.

Vectors are hand-built (AxisEmbedder) so the vector branch is predictable, and contents
are chosen so the full-text branch is too. Data is committed through real sessions, so
tables are truncated around each test.
"""

import uuid
from collections.abc import Iterator

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.embeddings.fake import FakeEmbedder
from app.main import create_app
from app.models import EMBEDDING_DIMENSION, Document, DocumentChunk
from app.services.users import get_or_create_user

pytestmark = pytest.mark.integration

URL = "/api/v1/search/hybrid"
ALICE, BOB = "alice@example.com", "bob@example.com"
MODEL = "test/axes"


def vec(*weights: float) -> np.ndarray:
    v = np.zeros(EMBEDDING_DIMENSION, dtype=np.float32)
    v[: len(weights)] = weights
    return v / np.linalg.norm(v)


class AxisEmbedder(FakeEmbedder):
    """Every query embeds to axis 0 unless it names another axis explicitly."""

    model_name = MODEL

    def embed_query(self, text: str) -> np.ndarray:
        if text.startswith("axis1"):
            return vec(0.0, 1.0)
        return vec(1.0)


@pytest.fixture
def settings(test_database_url: str) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        database_url=test_database_url,
        embedding_backend="fake",
        dev_user_email=ALICE,
    )


@pytest.fixture(autouse=True)
def clean(pg_engine: Engine) -> Iterator[None]:
    def truncate() -> None:
        with pg_engine.begin() as conn:
            conn.execute(text("TRUNCATE users CASCADE"))

    truncate()
    yield
    truncate()


def _client(settings: Settings) -> TestClient:
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.embedder = AxisEmbedder(EMBEDDING_DIMENSION)
    return TestClient(app, raise_server_exceptions=False)


def _seed(
    engine: Engine, email: str, chunks: list[tuple[str, np.ndarray]], model: str = MODEL
) -> uuid.UUID:
    with Session(engine, expire_on_commit=False) as session:
        user = get_or_create_user(session, email)
        document = Document(
            user_id=user.id,
            filename=f"{email.split('@')[0]}.txt",
            content_type="text/plain",
            file_size=1,
            content_hash=uuid.uuid4().hex * 2,
        )
        session.add(document)
        session.flush()
        for index, (content, vector) in enumerate(chunks):
            session.add(
                DocumentChunk(
                    document_id=document.id,
                    chunk_index=index,
                    content=content,
                    char_start=0,
                    char_end=len(content),
                    token_count=3,
                    embedding=vector,
                    embedding_model=model,
                )
            )
        session.commit()
        return document.id


def _post(client: TestClient, **body) -> dict:
    response = client.post(URL, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_fusion_promotes_agreement_and_reports_provenance(settings, pg_engine) -> None:
    _seed(
        pg_engine,
        ALICE,
        [
            ("Delivery takes three days.", vec(1.0)),  # vector rank 1, no term match
            ("Invoices must be paid within 30 days.", vec(1.0, 0.5)),  # vector 2 + text
            ("Invoice numbers are printed on top.", vec(0.0, 1.0)),  # vector 3 + text
        ],
    )
    with _client(settings) as client:
        body = _post(client, query="invoice paid", top_k=3)

    results = body["results"]
    assert [r["content"] for r in results] == [
        "Invoices must be paid within 30 days.",
        "Invoice numbers are printed on top.",
        "Delivery takes three days.",
    ]
    top = results[0]
    assert (top["vector_rank"], top["full_text_rank"]) == (2, 1)
    assert top["vector_similarity"] == pytest.approx(1 / np.sqrt(1.25), abs=1e-5)
    assert top["full_text_score"] > 0
    assert top["rrf_score"] == pytest.approx(1 / 62 + 1 / 61)
    last = results[2]
    assert (last["full_text_rank"], last["full_text_score"]) == (None, None)


def test_mode_vector_matches_semantic_endpoint_order(settings, pg_engine) -> None:
    _seed(pg_engine, ALICE, [(f"chunk {i}", vec(1.0, float(i))) for i in range(6)])
    with _client(settings) as client:
        semantic = client.post("/api/v1/search", json={"query": "q", "top_k": 5}).json()
        hybrid = _post(client, query="q", top_k=5, mode="vector")
    assert [r["chunk_id"] for r in hybrid["results"]] == [
        r["chunk_id"] for r in semantic["results"]
    ]
    assert [r["vector_similarity"] for r in hybrid["results"]] == pytest.approx(
        [r["similarity"] for r in semantic["results"]]
    )


def test_full_text_only_and_vector_only_fallbacks(settings, pg_engine) -> None:
    _seed(
        pg_engine,
        ALICE,
        [("Сумма счёта указана в тенге.", vec(1.0)), ("Кітапхана сенбіде ашық.", vec(0.0, 1.0))],
    )
    with _client(settings) as client:
        # No term matches: hybrid returns the vector branch alone.
        no_terms = _post(client, query="zebra", top_k=5)
        text_only = _post(client, query="кітапхана", mode="full_text")
        punctuation = _post(client, query="?!...", mode="full_text")
    assert [r["full_text_rank"] for r in no_terms["results"]] == [None, None]
    assert [r["vector_rank"] for r in no_terms["results"]] == [1, 2]
    assert [r["content"] for r in text_only["results"]] == ["Кітапхана сенбіде ашық."]
    assert text_only["results"][0]["vector_similarity"] is None
    assert punctuation["results"] == []


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("invoices", "Invoices are issued monthly."),  # EN stemming
        ("счета", "Счёт выставляется ежемесячно."),  # RU stemming + ё
        ("кітапхана", "Кітапхана сенбіде жұмыс істейді."),  # KK exact form
    ],
)
def test_multilingual_full_text(settings, pg_engine, query: str, expected: str) -> None:
    _seed(
        pg_engine,
        ALICE,
        [
            ("Invoices are issued monthly.", vec(1.0)),
            ("Счёт выставляется ежемесячно.", vec(1.0)),
            ("Кітапхана сенбіде жұмыс істейді.", vec(1.0)),
        ],
    )
    with _client(settings) as client:
        body = _post(client, query=query, mode="full_text")
    assert [r["content"] for r in body["results"]] == [expected]


def test_top_k_and_candidate_pool_limits(settings, pg_engine) -> None:
    _seed(pg_engine, ALICE, [(f"invoice line {i}", vec(1.0, float(i))) for i in range(12)])
    with _client(settings) as client:
        body = _post(client, query="invoice", top_k=3, candidate_pool=4)
        assert len(body["results"]) == 3
        assert body["candidate_pool"] == 4
        ranks = [r[k] for r in body["results"] for k in ("vector_rank", "full_text_rank")]
        assert all(rank is None or rank <= 4 for rank in ranks)
        too_small = client.post(URL, json={"query": "q", "top_k": 5, "candidate_pool": 2})
        too_big = client.post(URL, json={"query": "q", "candidate_pool": 101})
    assert too_small.status_code == too_big.status_code == 422


def test_document_filter_applies_to_both_branches(settings, pg_engine) -> None:
    wanted = _seed(pg_engine, ALICE, [("invoice in wanted document", vec(0.0, 1.0))])
    _seed(pg_engine, ALICE, [("invoice in other document", vec(1.0))])
    with _client(settings) as client:
        body = _post(client, query="invoice", document_ids=[str(wanted)])
    assert {r["document_id"] for r in body["results"]} == {str(wanted)}
    assert body["results"][0]["vector_rank"] == 1 and body["results"][0]["full_text_rank"] == 1


def test_two_users_are_isolated_in_both_branches(settings, pg_engine) -> None:
    bob_doc = _seed(pg_engine, BOB, [("secret invoice of bob", vec(1.0))])
    _seed(pg_engine, ALICE, [("alice note", vec(0.0, 1.0))])
    with _client(settings) as alice:
        # Bob's chunk is the best match for both branches, yet never appears for Alice.
        body = _post(alice, query="secret invoice bob", top_k=20)
        foreign = alice.post(URL, json={"query": "invoice", "document_ids": [str(bob_doc)]})
        unknown = alice.post(URL, json={"query": "invoice", "document_ids": [str(uuid.uuid4())]})
    assert [r["content"] for r in body["results"]] == ["alice note"]
    assert body["results"][0]["full_text_rank"] is None
    assert foreign.status_code == unknown.status_code == 404
    codes = {foreign.json()["error"]["code"], unknown.json()["error"]["code"]}
    assert codes == {"document_not_found"}
    assert "bob" not in foreign.text.replace(str(bob_doc), "")

    with _client(settings.model_copy(update={"dev_user_email": BOB})) as bob:
        assert [r["content"] for r in _post(bob, query="invoice")["results"]] == [
            "secret invoice of bob"
        ]


def test_chunks_of_another_model_are_ineligible_in_both_branches(settings, pg_engine) -> None:
    _seed(pg_engine, ALICE, [("invoice from the old model", vec(1.0))], model="old/model")
    with _client(settings) as client:
        assert _post(client, query="invoice")["results"] == []
        assert _post(client, query="invoice", mode="full_text")["results"] == []


def test_empty_index_returns_empty_results(settings) -> None:
    with _client(settings) as client:
        assert _post(client, query="anything")["results"] == []


def test_statement_timeout_returns_503(settings, pg_engine) -> None:
    _seed(pg_engine, ALICE, [("invoice", vec(1.0))])
    fast_fail = settings.model_copy(update={"search_timeout_ms": 200})
    with _client(fast_fail) as client, pg_engine.connect() as conn:
        transaction = conn.begin()
        conn.execute(text("LOCK TABLE document_chunks IN ACCESS EXCLUSIVE MODE"))
        blocked = client.post(URL, json={"query": "invoice", "mode": "full_text"})
        transaction.rollback()
        after = client.post(URL, json={"query": "invoice", "mode": "full_text"})
    assert blocked.status_code == 503
    assert blocked.json()["error"]["code"] == "database_unavailable"
    assert after.status_code == 200 and len(after.json()["results"]) == 1


def test_unreachable_database_is_503() -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        embedding_backend="fake",
        database_url="postgresql+psycopg://docmind:SECRETPW@127.0.0.1:1/docmind_test",
        db_connect_timeout_s=1,
    )
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(URL, json={"query": "q", "mode": "full_text"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "database_unavailable"
    assert "SECRETPW" not in response.text
