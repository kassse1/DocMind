"""POST /api/v1/search without a database: dependencies and the service are replaced."""

import uuid
from collections.abc import Iterator
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_current_user
from app.core.config import Settings, get_settings
from app.core.database import get_db_session
from app.embeddings.fake import FakeEmbedder
from app.main import create_app
from app.models import EMBEDDING_DIMENSION, User
from app.retrieval.vector_search import ChunkHit
from app.schemas.search import MAX_TOP_K
from app.services import search as search_service

URL = "/api/v1/search"
USER = User(id=uuid.uuid4(), email="search@example.com")


@pytest.fixture
def app_and_client(settings: Settings) -> Iterator[tuple[Any, TestClient]]:
    settings = settings.model_copy(update={"embedding_backend": "fake"})
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_current_user] = lambda: USER
    app.dependency_overrides[get_db_session] = lambda: None
    with TestClient(app, raise_server_exceptions=False) as client:
        yield app, client


@pytest.fixture
def api(app_and_client) -> TestClient:
    return app_and_client[1]


@pytest.fixture
def service_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    hit = ChunkHit(
        document_id=uuid.uuid4(),
        filename="contract.pdf",
        chunk_id=uuid.uuid4(),
        chunk_index=3,
        page_number=2,
        char_start=10,
        char_end=42,
        content="Payment is due within thirty days.",
        similarity=0.8125,
    )

    def fake_search(session, embedder, settings, user, request) -> list[ChunkHit]:
        calls.append({"embedder": embedder, "user": user, "request": request})
        return [hit]

    monkeypatch.setattr(search_service, "search", fake_search)
    return calls


def test_search_returns_results_with_metadata(api: TestClient, service_calls) -> None:
    response = api.post(URL, json={"query": "when is payment due?", "top_k": 3})

    assert response.status_code == 200
    body = response.json()
    assert body["metric"] == "cosine"
    assert body["embedding_model"] == "fake/sha256"
    [result] = body["results"]
    assert set(result) == {
        "document_id", "filename", "chunk_id", "chunk_index", "page_number",
        "char_start", "char_end", "content", "similarity",
    }
    assert (result["filename"], result["chunk_index"], result["page_number"]) == (
        "contract.pdf", 3, 2,
    )
    assert result["similarity"] == 0.8125
    [call] = service_calls
    assert (call["user"], call["request"].top_k) == (USER, 3)


def test_embedder_is_created_once_and_shared(app_and_client, service_calls) -> None:
    app, api = app_and_client
    api.post(URL, json={"query": "one"})
    api.post(URL, json={"query": "two"})
    assert service_calls[0]["embedder"] is service_calls[1]["embedder"] is app.state.embedder
    assert app.state.embedder.dimension == EMBEDDING_DIMENSION


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"query": ""},
        {"query": "   "},
        {"query": "x" * 1001},
        {"query": "q", "top_k": 0},
        {"query": "q", "top_k": MAX_TOP_K + 1},
        {"query": "q", "top_k": "many"},
        {"query": "q", "document_ids": ["not-a-uuid"]},
        {"query": "q", "document_ids": []},
        {"query": "q", "min_similarity": 2},
        {"query": "q", "unexpected": True},
    ],
)
def test_invalid_requests_are_422_in_error_envelope(
    api: TestClient, service_calls, payload: dict
) -> None:
    response = api.post(URL, json=payload)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert service_calls == []


def test_validation_error_does_not_echo_the_query(api: TestClient) -> None:
    response = api.post(URL, json={"query": "SECRET" * 200})
    assert response.status_code == 422
    assert "SECRET" not in response.text


def test_malformed_json_is_422(api: TestClient, service_calls) -> None:
    response = api.post(URL, content=b"{not json", headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_get_is_not_allowed(api: TestClient) -> None:
    response = api.get(URL, params={"query": "q"})
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"


def test_document_not_found_uses_error_envelope(api: TestClient, monkeypatch) -> None:
    missing = uuid.uuid4()
    monkeypatch.setattr(search_service, "_missing_documents", lambda *_: [missing])

    response = api.post(URL, json={"query": "q", "document_ids": [str(missing)]})

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "document_not_found",
            "message": "One or more documents were not found.",
            "details": {"document_ids": [str(missing)]},
        }
    }


class BrokenEmbedder(FakeEmbedder):
    def embed_query(self, text: str) -> np.ndarray:
        raise ImportError("No module named 'sentence_transformers'")


def test_unavailable_model_is_503(app_and_client) -> None:
    app, api = app_and_client
    app.state.embedder = BrokenEmbedder(EMBEDDING_DIMENSION)

    response = api.post(URL, json={"query": "q"})

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "embedding_unavailable"
    assert "sentence_transformers" not in response.text


def test_search_requires_authentication_in_prod(service_calls) -> None:
    settings = Settings(_env_file=None, environment="prod")
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_db_session] = lambda: None
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(URL, json={"query": "q"})
    assert response.status_code == 401
    assert service_calls == []
