"""Search service and query builder without a database (ranking itself is tested against
PostgreSQL in tests/integration/test_vector_search.py)."""

import re
import uuid
from typing import Any

import numpy as np
import pytest
from sqlalchemy.dialects import postgresql

from app.core.config import Settings
from app.core.errors import AppError
from app.embeddings.fake import FakeEmbedder
from app.models import EMBEDDING_DIMENSION, User
from app.retrieval.vector_search import build_vector_search_query
from app.schemas.search import SearchRequest
from app.services import search as search_service

USER_ID = uuid.uuid4()


class SpyEmbedder(FakeEmbedder):
    def __init__(self) -> None:
        super().__init__(EMBEDDING_DIMENSION)
        self.queries: list[str] = []

    def embed_query(self, text: str) -> np.ndarray:
        self.queries.append(text)
        return super().embed_query(text)


class StubSession:
    """Records statements; scalars() returns the ids this user "owns"."""

    def __init__(self, owned: set[uuid.UUID] | None = None) -> None:
        self.owned = owned or set()
        self.executed: list[Any] = []

    def scalars(self, _statement: Any) -> list[uuid.UUID]:
        return list(self.owned)

    def execute(self, statement: Any, params: Any = None) -> None:
        self.executed.append((statement, params))


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    recorded: list[dict[str, Any]] = []

    def fake_vector_search(_session: Any, vector: np.ndarray, **kwargs: Any) -> list[Any]:
        recorded.append({"vector": vector, **kwargs})
        return []

    monkeypatch.setattr(search_service, "vector_search", fake_vector_search)
    return recorded


def _settings() -> Settings:
    return Settings(_env_file=None, environment="test", search_timeout_ms=1234)


def _user() -> User:
    return User(id=USER_ID, email="search@example.com")


def _compile(statement: Any) -> tuple[str, dict[str, Any]]:
    compiled = statement.compile(dialect=postgresql.dialect())
    # Drop dialect casts (::UUID) so assertions read like the SQL shape they check.
    return re.sub(r"::\w+", "", " ".join(str(compiled).split())), compiled.params


def test_query_uses_cosine_distance_with_bound_vector_and_tie_breaker() -> None:
    vector = FakeEmbedder(EMBEDDING_DIMENSION).embed_query("hello")
    sql, params = _compile(
        build_vector_search_query(vector, user_id=USER_ID, embedding_model="m", top_k=7)
    )
    assert "document_chunks.embedding <=> %(embedding_1)s AS distance" in sql
    # The vector is sent once; ORDER BY reuses the selected distance.
    assert sql.count("%(embedding_1)s") == 1
    assert sql.endswith(
        "ORDER BY distance, document_chunks.document_id, document_chunks.chunk_index"
        " LIMIT %(param_1)s"
    )
    assert "documents.user_id = %(user_id_1)s" in sql
    assert "document_chunks.embedding_model = %(embedding_model_1)s" in sql
    assert params["user_id_1"] == USER_ID
    assert params["embedding_model_1"] == "m"
    assert params["param_1"] == 7
    assert np.array_equal(params["embedding_1"], vector)


def test_query_adds_document_filter_and_threshold_only_when_given() -> None:
    vector = np.ones(EMBEDDING_DIMENSION, dtype=np.float32)
    plain, _ = _compile(
        build_vector_search_query(vector, user_id=USER_ID, embedding_model="m", top_k=5)
    )
    assert " IN " not in plain and ") <= " not in plain

    doc = uuid.uuid4()
    sql, params = _compile(
        build_vector_search_query(
            vector,
            user_id=USER_ID,
            embedding_model="m",
            top_k=5,
            document_ids=[doc],
            min_similarity=0.25,
        )
    )
    assert "document_chunks.document_id IN (__[POSTCOMPILE_document_id_1])" in sql
    assert "(document_chunks.embedding <=> %(embedding_1)s) <= %(param_1)s" in sql
    assert params["document_id_1"] == [doc]
    # similarity >= 0.25  <=>  cosine distance <= 0.75
    assert params["param_1"] == pytest.approx(0.75)


def test_service_embeds_query_once_with_shared_embedder(calls) -> None:
    embedder = SpyEmbedder()
    session = StubSession()

    search_service.search(
        session, embedder, _settings(), _user(), SearchRequest(query="  payment terms ", top_k=3)
    )

    assert embedder.queries == ["payment terms"]
    [call] = calls
    assert call["vector"].shape == (EMBEDDING_DIMENSION,)
    np.testing.assert_allclose(np.linalg.norm(call["vector"]), 1.0, rtol=1e-5)
    assert call["embedding_model"] == embedder.model_name
    assert (call["user_id"], call["top_k"], call["document_ids"]) == (USER_ID, 3, None)
    # Statement timeout is set for the search transaction.
    [(_, params)] = session.executed
    assert params == {"t": "1234"}


def test_unknown_or_foreign_document_is_404_before_embedding(calls) -> None:
    owned, foreign = uuid.uuid4(), uuid.uuid4()
    embedder = SpyEmbedder()
    request = SearchRequest(query="q", document_ids=[owned, foreign])

    with pytest.raises(AppError) as exc_info:
        search_service.search(StubSession({owned}), embedder, _settings(), _user(), request)

    assert exc_info.value.status_code == 404
    assert exc_info.value.code == "document_not_found"
    assert exc_info.value.details == {"document_ids": [str(foreign)]}
    assert embedder.queries == [] and calls == []


def test_owned_document_filter_is_passed_through(calls) -> None:
    doc = uuid.uuid4()
    search_service.search(
        StubSession({doc}),
        SpyEmbedder(),
        _settings(),
        _user(),
        SearchRequest(query="q", document_ids=[doc], min_similarity=0.5),
    )
    assert (calls[0]["document_ids"], calls[0]["min_similarity"]) == ([doc], 0.5)


class BrokenEmbedder(FakeEmbedder):
    def embed_query(self, text: str) -> np.ndarray:
        raise OSError(f"model files missing for {text}")


class WrongDimensionEmbedder(FakeEmbedder):
    def embed_query(self, text: str) -> np.ndarray:
        return np.ones(8, dtype=np.float32)


@pytest.mark.parametrize("embedder_cls", [BrokenEmbedder, WrongDimensionEmbedder])
def test_embedding_failure_is_503_without_leaking_the_query(
    embedder_cls, calls, caplog
) -> None:
    with pytest.raises(AppError) as exc_info:
        search_service.search(
            StubSession(),
            embedder_cls(EMBEDDING_DIMENSION),
            _settings(),
            _user(),
            SearchRequest(query="SECRETQUERY"),
        )
    assert (exc_info.value.status_code, exc_info.value.code) == (503, "embedding_unavailable")
    assert "SECRETQUERY" not in exc_info.value.message
    assert calls == []
    assert all("SECRETQUERY" not in r.getMessage() for r in caplog.records)


def test_logs_do_not_contain_query_text(calls, caplog) -> None:
    caplog.set_level("DEBUG")
    search_service.search(
        StubSession(), SpyEmbedder(), _settings(), _user(), SearchRequest(query="SECRETQUERY")
    )
    messages = "\n".join(r.getMessage() for r in caplog.records)
    assert "Search: user=" in messages
    assert "SECRETQUERY" not in messages
