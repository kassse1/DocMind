"""Hybrid search schemas, service branching and API wiring without a database."""

import uuid
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

from app.api.deps import get_current_user
from app.core.config import Settings, get_settings
from app.core.database import get_db_session
from app.core.errors import AppError
from app.embeddings.fake import FakeEmbedder
from app.main import create_app
from app.models import EMBEDDING_DIMENSION, User
from app.retrieval.full_text_search import FullTextHit
from app.retrieval.vector_search import ChunkHit
from app.schemas.search import MAX_CANDIDATE_POOL, HybridSearchRequest
from app.services import search as search_service

USER = User(id=uuid.uuid4(), email="hybrid@example.com")
DOC = uuid.uuid4()
SHARED = uuid.uuid4()


# --- schema -----------------------------------------------------------------------------


def test_hybrid_request_defaults() -> None:
    request = HybridSearchRequest(query=" q ")
    assert (request.query, request.mode, request.top_k, request.candidate_pool) == (
        "q",
        "hybrid",
        5,
        None,
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"query": ""},
        {"query": "q", "mode": "bm25"},
        {"query": "q", "candidate_pool": 0},
        {"query": "q", "candidate_pool": MAX_CANDIDATE_POOL + 1},
        {"query": "q", "top_k": 10, "candidate_pool": 5},
        {"query": "q", "min_similarity": 0.5},  # cosine threshold not offered for fused ranks
        {"query": "q", "top_k": 21},
        {"query": "q", "document_ids": []},
    ],
)
def test_invalid_hybrid_requests(payload: dict) -> None:
    with pytest.raises(ValidationError):
        HybridSearchRequest(**payload)


def test_candidate_pool_bounds_accepted() -> None:
    assert HybridSearchRequest(query="q", top_k=20, candidate_pool=20).candidate_pool == 20
    assert HybridSearchRequest(query="q", candidate_pool=MAX_CANDIDATE_POOL).candidate_pool == 100


# --- service ------------------------------------------------------------------------------


class SpyEmbedder(FakeEmbedder):
    def __init__(self) -> None:
        super().__init__(EMBEDDING_DIMENSION)
        self.calls = 0

    def embed_query(self, text: str) -> np.ndarray:
        self.calls += 1
        return super().embed_query(text)


class StubSession:
    def __init__(self, owned: set[uuid.UUID] | None = None) -> None:
        self.owned = owned or set()

    def scalars(self, _statement: Any) -> list[uuid.UUID]:
        return list(self.owned)

    def execute(self, *_args: Any) -> None:
        return None


@pytest.fixture
def branches(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[dict]]:
    calls: dict[str, list[dict]] = {"vector": [], "full_text": []}

    def fake_vector(_session, vector, **kwargs) -> list[ChunkHit]:
        calls["vector"].append({"vector": vector, **kwargs})
        return [
            ChunkHit(DOC, "a.txt", uuid.UUID(int=1), 0, None, 0, 1, "vector only", 0.9),
            ChunkHit(DOC, "a.txt", SHARED, 1, 3, 5, 9, "both", 0.8),
        ]

    def fake_text(_session, query_text, **kwargs) -> list[FullTextHit]:
        calls["full_text"].append({"query": query_text, **kwargs})
        return [FullTextHit(DOC, "a.txt", SHARED, 1, 3, 5, 9, "both", 0.2)]

    monkeypatch.setattr(search_service, "vector_search", fake_vector)
    monkeypatch.setattr(search_service, "full_text_search", fake_text)
    return calls


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, environment="test", **overrides)


def _run(request: HybridSearchRequest, embedder=None, session=None, **settings):
    return search_service.hybrid_search(
        session or StubSession(), embedder or SpyEmbedder(), _settings(**settings), USER, request
    )


def test_hybrid_runs_both_branches_with_same_scope_and_pool(branches) -> None:
    embedder = SpyEmbedder()
    outcome = _run(HybridSearchRequest(query="q", top_k=3), embedder, hybrid_candidate_pool=40)

    assert embedder.calls == 1
    [vec_call], [text_call] = branches["vector"], branches["full_text"]
    for call in (vec_call, text_call):
        assert call["user_id"] == USER.id
        assert call["embedding_model"] == embedder.model_name
        assert call["document_ids"] is None
    assert vec_call["top_k"] == text_call["limit"] == 40
    assert "min_similarity" not in vec_call
    assert outcome.candidate_pool == 40 and outcome.rrf_k == 60
    assert [h.content for h in outcome.hits] == ["both", "vector only"]
    both = outcome.hits[0]
    assert (both.vector_rank, both.full_text_rank, both.page_number) == (2, 1, 3)


def test_pool_is_never_smaller_than_top_k_and_request_overrides_setting(branches) -> None:
    outcome = _run(HybridSearchRequest(query="q", top_k=20), hybrid_candidate_pool=5)
    assert outcome.candidate_pool == 20
    assert _run(HybridSearchRequest(query="q", candidate_pool=7)).candidate_pool == 7


def test_full_text_mode_needs_no_model(branches) -> None:
    class NoModel(FakeEmbedder):
        def embed_query(self, text: str) -> np.ndarray:
            raise ImportError("sentence_transformers missing")

    outcome = _run(HybridSearchRequest(query="q", mode="full_text"), NoModel(EMBEDDING_DIMENSION))
    assert branches["vector"] == []
    assert [h.content for h in outcome.hits] == ["both"]
    assert outcome.hits[0].vector_similarity is None


def test_vector_mode_skips_full_text(branches) -> None:
    outcome = _run(HybridSearchRequest(query="q", mode="vector"))
    assert branches["full_text"] == []
    assert [h.full_text_rank for h in outcome.hits] == [None, None]


def test_unavailable_model_fails_hybrid_instead_of_degrading(branches) -> None:
    class NoModel(FakeEmbedder):
        def embed_query(self, text: str) -> np.ndarray:
            raise OSError("not cached")

    with pytest.raises(AppError) as exc_info:
        _run(HybridSearchRequest(query="q"), NoModel(EMBEDDING_DIMENSION))
    assert exc_info.value.code == "embedding_unavailable"
    assert branches == {"vector": [], "full_text": []}


def test_foreign_document_filter_is_404_before_any_branch(branches) -> None:
    embedder = SpyEmbedder()
    with pytest.raises(AppError) as exc_info:
        _run(HybridSearchRequest(query="q", document_ids=[uuid.uuid4()]), embedder)
    assert exc_info.value.status_code == 404
    assert exc_info.value.code == "document_not_found"
    assert embedder.calls == 0 and branches == {"vector": [], "full_text": []}


def test_owned_document_filter_reaches_both_branches(branches) -> None:
    _run(HybridSearchRequest(query="q", document_ids=[DOC]), session=StubSession({DOC}))
    assert branches["vector"][0]["document_ids"] == [DOC]
    assert branches["full_text"][0]["document_ids"] == [DOC]


def test_logs_never_contain_query_or_passages(branches, caplog) -> None:
    caplog.set_level("DEBUG")
    _run(HybridSearchRequest(query="SECRETQUERY"))
    messages = "\n".join(r.getMessage() for r in caplog.records)
    assert "Hybrid search: user=" in messages
    assert "SECRETQUERY" not in messages and "vector only" not in messages


# --- API ----------------------------------------------------------------------------------


@pytest.fixture
def api(settings: Settings):
    settings = settings.model_copy(update={"embedding_backend": "fake"})
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_current_user] = lambda: USER
    app.dependency_overrides[get_db_session] = lambda: StubSession()
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def test_hybrid_endpoint_response_contract(api: TestClient, branches) -> None:
    response = api.post("/api/v1/search/hybrid", json={"query": "q", "top_k": 2})
    assert response.status_code == 200
    body = response.json()
    assert {k: body[k] for k in ("mode", "fusion", "rrf_k", "candidate_pool")} == {
        "mode": "hybrid",
        "fusion": "rrf",
        "rrf_k": 60,
        "candidate_pool": 50,
    }
    assert body["full_text_configs"] == ["simple", "russian"]
    assert body["embedding_model"] == "fake/sha256"
    first, second = body["results"]
    assert set(first) == {
        "document_id", "filename", "chunk_id", "chunk_index", "page_number", "char_start",
        "char_end", "content", "rrf_score", "vector_rank", "vector_similarity",
        "full_text_rank", "full_text_score",
    }
    assert (first["vector_rank"], first["full_text_rank"]) == (2, 1)
    # Present-but-null when the branch did not return the chunk.
    assert (second["full_text_rank"], second["full_text_score"]) == (None, None)
    assert "similarity" not in first  # the cosine field name is not reused


def test_hybrid_endpoint_validation_uses_error_envelope(api: TestClient, branches) -> None:
    response = api.post("/api/v1/search/hybrid", json={"query": "q", "min_similarity": 0.3})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_hybrid_endpoint_maps_database_failures_to_503(api: TestClient, monkeypatch) -> None:
    def broken(*_args, **_kwargs):
        raise OperationalError("SELECT", {}, Exception("host=10.0.0.1 password=SECRET"))

    monkeypatch.setattr(search_service, "full_text_search", broken)
    response = api.post("/api/v1/search/hybrid", json={"query": "q", "mode": "full_text"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "database_unavailable"
    assert "SECRET" not in response.text


def test_semantic_endpoint_contract_is_unchanged(api: TestClient) -> None:
    schema = api.get("/openapi.json").json()["components"]["schemas"]
    assert list(schema["SearchRequest"]["properties"]) == [
        "query", "top_k", "document_ids", "min_similarity",
    ]
    assert schema["SearchRequest"]["required"] == ["query"]
    assert set(schema["SearchResult"]["properties"]) == {
        "document_id", "filename", "chunk_id", "chunk_index", "page_number", "char_start",
        "char_end", "content", "similarity",
    }
    assert set(schema["SearchResponse"]["properties"]) == {
        "results", "metric", "embedding_model",
    }
