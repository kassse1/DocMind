"""POST /api/v1/search end to end against the real test database.

The app and its dev-user dependency run unmodified; data is committed, so tables are
truncated around each test (like the upload and indexing tests).
"""

import uuid
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import create_session_factory
from app.embeddings.fake import FakeEmbedder
from app.ingestion.storage import FileStorage
from app.main import create_app
from app.models import EMBEDDING_DIMENSION, Document, DocumentChunk
from app.services.indexing import DocumentIndexer
from app.services.users import get_or_create_user

pytestmark = pytest.mark.integration

URL = "/api/v1/search"
ALICE = "alice@example.com"
BOB = "bob@example.com"


def vec(*weights: float) -> np.ndarray:
    v = np.zeros(EMBEDDING_DIMENSION, dtype=np.float32)
    v[: len(weights)] = weights
    return v / np.linalg.norm(v)


class AxisEmbedder(FakeEmbedder):
    """Queries "a", "b", "c" map to fixed axes, so expected rankings are exact."""

    model_name = "test/axes"
    AXES = {"a": vec(1.0), "b": vec(0.0, 1.0), "c": vec(0.0, 0.0, 1.0)}

    def embed_query(self, text: str) -> np.ndarray:
        return self.AXES[text]


def _truncate(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE users CASCADE"))


@pytest.fixture
def settings(test_database_url: str, tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        database_url=test_database_url,
        storage_dir=tmp_path / "storage",
        embedding_backend="fake",
        dev_user_email=ALICE,
        chunk_size_tokens=16,
        chunk_overlap_tokens=4,
    )


@pytest.fixture
def clean(pg_engine: Engine) -> Iterator[None]:
    _truncate(pg_engine)
    yield
    _truncate(pg_engine)


def _client(settings: Settings, embedder=None) -> TestClient:
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    if embedder is not None:
        app.state.embedder = embedder
    return TestClient(app, raise_server_exceptions=False)


def _seed(pg_engine: Engine, email: str, filename: str, vectors: list[np.ndarray]) -> uuid.UUID:
    """A document of `email` with one chunk per vector, chunk i saying "<filename> #i"."""
    with Session(pg_engine, expire_on_commit=False) as session:
        user = get_or_create_user(session, email)  # commits on its own
        document = Document(
            user_id=user.id,
            filename=filename,
            content_type="text/plain",
            file_size=1,
            content_hash=uuid.uuid4().hex * 2,
        )
        session.add(document)
        session.flush()
        for index, vector in enumerate(vectors):
            content = f"{filename} #{index}"
            session.add(
                DocumentChunk(
                    document_id=document.id,
                    chunk_index=index,
                    content=content,
                    char_start=0,
                    char_end=len(content),
                    token_count=2,
                    embedding=vector,
                    embedding_model=AxisEmbedder.model_name,
                )
            )
        session.commit()
        return document.id


def test_ranking_scores_and_filters_through_the_api(settings, pg_engine, clean) -> None:
    doc_a = _seed(pg_engine, ALICE, "a.txt", [vec(1.0), vec(0.0, 1.0)])
    doc_b = _seed(pg_engine, ALICE, "b.txt", [vec(1.0, 1.0)])

    with _client(settings, AxisEmbedder(EMBEDDING_DIMENSION)) as client:
        response = client.post(URL, json={"query": "a", "top_k": 5})
        assert response.status_code == 200
        body = response.json()
        assert (body["metric"], body["embedding_model"]) == ("cosine", "test/axes")
        results = body["results"]
        assert [r["content"] for r in results] == ["a.txt #0", "b.txt #0", "a.txt #1"]
        assert [r["similarity"] for r in results] == pytest.approx(
            [1.0, 1 / np.sqrt(2), 0.0], abs=1e-5
        )
        assert results[0]["document_id"] == str(doc_a)
        assert results[0]["filename"] == "a.txt"

        assert len(client.post(URL, json={"query": "a", "top_k": 1}).json()["results"]) == 1

        filtered = client.post(URL, json={"query": "a", "document_ids": [str(doc_b)]})
        assert [r["document_id"] for r in filtered.json()["results"]] == [str(doc_b)]

        thresholded = client.post(URL, json={"query": "a", "min_similarity": 0.9})
        assert [r["content"] for r in thresholded.json()["results"]] == ["a.txt #0"]

        unrelated = client.post(URL, json={"query": "c", "min_similarity": 0.5})
        assert unrelated.json()["results"] == []


def test_empty_index_returns_empty_results(settings, clean) -> None:
    with _client(settings, AxisEmbedder(EMBEDDING_DIMENSION)) as client:
        response = client.post(URL, json={"query": "a"})
    assert response.status_code == 200
    assert response.json()["results"] == []


def test_users_only_see_their_own_documents(settings, pg_engine, clean) -> None:
    bob_doc = _seed(pg_engine, BOB, "bob-private.txt", [vec(1.0)])
    _seed(pg_engine, ALICE, "alice.txt", [vec(0.0, 1.0)])
    embedder = AxisEmbedder(EMBEDDING_DIMENSION)

    with _client(settings, embedder) as alice:
        results = alice.post(URL, json={"query": "a"}).json()["results"]
        assert [r["filename"] for r in results] == ["alice.txt"]

        # Naming Bob's document is answered exactly like a nonexistent one.
        foreign = alice.post(URL, json={"query": "a", "document_ids": [str(bob_doc)]})
        unknown_id = str(uuid.uuid4())
        unknown = alice.post(URL, json={"query": "a", "document_ids": [unknown_id]})
        assert foreign.status_code == unknown.status_code == 404
        assert foreign.json()["error"]["code"] == "document_not_found"
        assert "bob-private" not in foreign.text

    bob_settings = settings.model_copy(update={"dev_user_email": BOB})
    with _client(bob_settings, embedder) as bob:
        results = bob.post(URL, json={"query": "a"}).json()["results"]
        assert [r["filename"] for r in results] == ["bob-private.txt"]


def test_upload_index_then_search(settings, pg_engine, clean) -> None:
    """Full pipeline with the fake embedder: scores are meaningless, the wiring is not."""
    paragraphs = ["First paragraph about payments.", "Second paragraph about delivery."]
    with _client(settings) as client:
        upload = client.post(
            "/api/v1/documents",
            files={"file": ("terms.txt", "\n\n".join(paragraphs).encode(), "text/plain")},
        )
        assert upload.status_code == 201
        doc_id = upload.json()["document"]["id"]

        # Not indexed yet: nothing to find.
        assert client.post(URL, json={"query": "payments"}).json()["results"] == []

        indexer = DocumentIndexer(
            create_session_factory(pg_engine),
            FileStorage(settings.storage_dir),
            FakeEmbedder(EMBEDDING_DIMENSION),
            settings,
        )
        assert indexer.run_once() is True

        response = client.post(URL, json={"query": "payments", "top_k": 5})
    assert response.status_code == 200
    body = response.json()
    assert body["embedding_model"] == "fake/sha256"
    results = body["results"]
    with Session(pg_engine) as session:
        chunk_count = session.query(DocumentChunk).count()
    assert 0 < len(results) == chunk_count
    assert {r["document_id"] for r in results} == {doc_id}
    assert {r["filename"] for r in results} == {"terms.txt"}
    assert all(-1.0 <= r["similarity"] <= 1.0 for r in results)
    assert [r["similarity"] for r in results] == sorted(
        (r["similarity"] for r in results), reverse=True
    )


def test_statement_timeout_returns_503_instead_of_hanging(settings, pg_engine, clean) -> None:
    _seed(pg_engine, ALICE, "a.txt", [vec(1.0)])
    fast_fail = settings.model_copy(update={"search_timeout_ms": 200})

    with _client(fast_fail, AxisEmbedder(EMBEDDING_DIMENSION)) as client:
        # Another transaction blocks all reads of document_chunks until it ends.
        with pg_engine.connect() as conn:
            transaction = conn.begin()
            conn.execute(text("LOCK TABLE document_chunks IN ACCESS EXCLUSIVE MODE"))
            blocked = client.post(URL, json={"query": "a"})
            transaction.rollback()
        # The timeout is transaction-local: the pooled connection is healthy afterwards.
        after = client.post(URL, json={"query": "a"})

    assert blocked.status_code == 503
    assert blocked.json()["error"]["code"] == "database_unavailable"
    assert after.status_code == 200
    assert len(after.json()["results"]) == 1
