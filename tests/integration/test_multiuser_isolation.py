"""End-to-end multi-user isolation over HTTP: Alice and Bob register, log in, upload,
get indexed by the real worker, then search, ask, list, read and delete. Every request
carries a real token obtained from /auth/login; no test shortcut provides identity."""

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.core.config import Settings, get_settings
from app.core.database import create_session_factory
from app.core.security import create_access_token
from app.embeddings.fake import FakeEmbedder
from app.ingestion.storage import FileStorage
from app.main import create_app
from app.models import EMBEDDING_DIMENSION
from app.rag.generation import FakeGenerator
from app.services.indexing import DocumentIndexer
from tests.conftest import TEST_JWT_SECRET

pytestmark = pytest.mark.integration

PASSWORD = "synthetic passphrase for tests"
ALICE_TEXT = "Alice's warehouse lease costs 450 000 tenge per month.\n\nAlice pays by the 5th."
BOB_TEXT = "Bob's gym membership costs 45 dollars per month.\n\nBob may freeze it for two months."


@pytest.fixture
def settings(test_database_url: str, tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None, environment="test", database_url=test_database_url,
        storage_dir=tmp_path / "storage", embedding_backend="fake", generation_backend="fake",
        jwt_secret_key=TEST_JWT_SECRET,
    )


@pytest.fixture(autouse=True)
def clean(pg_engine: Engine) -> Iterator[None]:
    def truncate() -> None:
        with pg_engine.begin() as conn:
            conn.execute(text("TRUNCATE users CASCADE"))

    truncate()
    yield
    truncate()


class World:
    """Two authenticated clients sharing one app, plus the uploaded document ids."""

    def __init__(self, settings: Settings, pg_engine: Engine) -> None:
        self.app = create_app(settings)
        self.app.dependency_overrides[get_settings] = lambda: settings
        self.generator = FakeGenerator()
        self.app.state.generator = self.generator
        self.settings, self.engine = settings, pg_engine
        self.anonymous = TestClient(self.app, raise_server_exceptions=False)
        self.alice = self._user("alice@example.com")
        self.bob = self._user("bob@example.com")

    def _user(self, email: str) -> TestClient:
        client = TestClient(self.app, raise_server_exceptions=False)
        assert client.post(
            "/api/v1/auth/register", json={"email": email, "password": PASSWORD}
        ).status_code == 201
        login = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
        client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
        return client

    def upload(self, client: TestClient, name: str, content: str) -> str:
        response = client.post(
            "/api/v1/documents", files={"file": (name, content.encode(), "text/plain")}
        )
        assert response.status_code == 201, response.text
        return response.json()["document"]["id"]

    def index_all(self) -> None:
        indexer = DocumentIndexer(
            create_session_factory(self.engine), FileStorage(self.settings.storage_dir),
            FakeEmbedder(EMBEDDING_DIMENSION), self.settings,
        )
        while indexer.run_once():
            pass


@pytest.fixture
def world(settings, pg_engine) -> Iterator[World]:
    w = World(settings, pg_engine)
    w.alice_doc = w.upload(w.alice, "alice-lease.txt", ALICE_TEXT)
    w.bob_doc = w.upload(w.bob, "bob-gym.txt", BOB_TEXT)
    w.index_all()
    yield w
    for client in (w.anonymous, w.alice, w.bob):
        client.close()


def _not_found(response) -> dict:
    assert response.status_code == 404, response.text
    return response.json()["error"]


def test_worker_indexes_each_document_for_its_own_owner(world, pg_engine) -> None:
    with pg_engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT u.email, d.filename, d.status, count(c.id) FROM users u "
            "JOIN documents d ON d.user_id = u.id JOIN document_chunks c ON c.document_id = d.id "
            "GROUP BY 1, 2, 3 ORDER BY 1"
        )).all()
    assert [(r[0], r[1], r[2]) for r in rows] == [
        ("alice@example.com", "alice-lease.txt", "ready"),
        ("bob@example.com", "bob-gym.txt", "ready"),
    ]
    assert all(r[3] > 0 for r in rows)


def test_list_and_detail_show_only_own_documents(world) -> None:
    listed = world.alice.get("/api/v1/documents").json()["documents"]
    assert [d["id"] for d in listed] == [world.alice_doc]
    assert world.alice.get(f"/api/v1/documents/{world.alice_doc}").json()["status"] == "ready"

    foreign = _not_found(world.alice.get(f"/api/v1/documents/{world.bob_doc}"))
    missing = _not_found(world.alice.get(f"/api/v1/documents/{uuid.uuid4()}"))
    assert foreign == missing == {
        "code": "document_not_found", "message": "Document not found.", "details": None,
    }


def test_users_cannot_delete_each_others_documents(world, pg_engine) -> None:
    _not_found(world.alice.delete(f"/api/v1/documents/{world.bob_doc}"))
    assert world.bob.get(f"/api/v1/documents/{world.bob_doc}").status_code == 200

    assert world.bob.delete(f"/api/v1/documents/{world.bob_doc}").status_code == 204
    _not_found(world.bob.get(f"/api/v1/documents/{world.bob_doc}"))
    with pg_engine.connect() as conn:
        assert conn.scalar(text(
            "SELECT count(*) FROM document_chunks WHERE document_id = :d"
        ), {"d": world.bob_doc}) == 0
        assert conn.scalar(text("SELECT count(*) FROM jobs WHERE document_id = :d"),
                           {"d": world.bob_doc}) == 0
    assert not FileStorage(world.settings.storage_dir).path_for(uuid.UUID(world.bob_doc)).exists()
    # Alice's document is untouched.
    assert world.alice.get(f"/api/v1/documents/{world.alice_doc}").status_code == 200


@pytest.mark.parametrize(
    ("url", "body"),
    [
        ("/api/v1/search", {"query": "monthly cost", "top_k": 20}),
        ("/api/v1/search/hybrid", {"query": "monthly cost", "top_k": 20}),
        ("/api/v1/search/hybrid", {"query": "costs per month", "top_k": 20, "mode": "full_text"}),
    ],
    ids=["semantic", "hybrid", "full-text"],
)
def test_search_returns_only_own_passages(world, url, body) -> None:
    for client, own, own_text, other_text in (
        (world.alice, world.alice_doc, "Alice", "Bob"),
        (world.bob, world.bob_doc, "Bob", "Alice"),
    ):
        results = client.post(url, json=body).json()["results"]
        assert results, url
        assert {r["document_id"] for r in results} == {own}
        assert all(own_text in r["content"] and other_text not in r["content"] for r in results)


def test_ask_prompts_contain_only_own_passages(world) -> None:
    for client, own_doc, own, other in (
        (world.alice, world.alice_doc, "Alice", "Bob"),
        (world.bob, world.bob_doc, "Bob", "Alice"),
    ):
        before = len(world.generator.calls)
        body = client.post("/api/v1/ask", json={"question": "What does it cost?"}).json()
        assert body["abstained"] is False
        assert {c["document_id"] for c in body["citations"]} == {own_doc}
        prompt = world.generator.calls[before][-1].content
        assert own in prompt and other not in prompt


def test_strict_grounding_mode_keeps_isolation(world, settings) -> None:
    strict = settings.model_copy(update={"rag_grounding_mode": "verified"})
    world.app.dependency_overrides[get_settings] = lambda: strict
    # The extractive fake cannot judge support: the verifier output is malformed, so the
    # answer fails closed. The point here is which passages each call received.
    body = world.alice.post("/api/v1/ask", json={"question": "What does it cost?"}).json()
    assert body["abstention_reason"] == "verification_inconclusive"
    for call in world.generator.calls:
        assert "Bob" not in "".join(m.content for m in call)


@pytest.mark.parametrize(
    ("url", "extra"),
    [("/api/v1/search", {"query": "cost"}), ("/api/v1/search/hybrid", {"query": "cost"}),
     ("/api/v1/ask", {"question": "cost?"})],
)
def test_foreign_missing_and_mixed_document_ids_give_the_same_404(world, url, extra) -> None:
    calls = len(world.generator.calls)
    foreign = _not_found(world.alice.post(url, json={**extra, "document_ids": [world.bob_doc]}))
    missing_id = str(uuid.uuid4())
    missing = _not_found(world.alice.post(url, json={**extra, "document_ids": [missing_id]}))
    mixed = _not_found(
        world.alice.post(url, json={**extra, "document_ids": [world.alice_doc, world.bob_doc]})
    )
    assert foreign["code"] == missing["code"] == mixed["code"] == "document_not_found"
    assert foreign["message"] == missing["message"]
    # Only the ids the caller named come back, the caller's own id is not listed as missing.
    assert mixed["details"] == {"document_ids": [world.bob_doc]}
    assert len(world.generator.calls) == calls  # nothing reached the model


def test_user_ids_in_requests_cannot_select_another_account(world) -> None:
    bob_id = world.bob.get("/api/v1/auth/me").json()["id"]
    # Extra body fields are rejected; headers and query parameters are ignored.
    assert world.alice.post(
        "/api/v1/search", json={"query": "cost", "user_id": bob_id}
    ).status_code == 422
    response = world.alice.post(
        "/api/v1/search", params={"user_id": bob_id}, headers={"X-User-ID": bob_id},
        json={"query": "cost", "top_k": 20},
    )
    assert {r["document_id"] for r in response.json()["results"]} == {world.alice_doc}
    listed = world.alice.get("/api/v1/documents", params={"user_id": bob_id}).json()
    assert [d["id"] for d in listed["documents"]] == [world.alice_doc]


PROTECTED = [
    ("GET", "/api/v1/auth/me", None),
    ("GET", "/api/v1/documents", None),
    ("GET", "/api/v1/documents/{doc}", None),
    ("DELETE", "/api/v1/documents/{doc}", None),
    ("POST", "/api/v1/search", {"query": "cost"}),
    ("POST", "/api/v1/search/hybrid", {"query": "cost"}),
    ("POST", "/api/v1/ask", {"question": "cost?"}),
]


@pytest.mark.parametrize(("method", "path", "body"), PROTECTED)
def test_protected_endpoints_reject_missing_and_expired_tokens(world, method, path, body):
    path = path.format(doc=world.alice_doc)
    claims = jwt.decode(
        world.alice.headers["Authorization"].split()[1], options={"verify_signature": False}
    )
    expired = create_access_token(
        uuid.UUID(claims["sub"]), uuid.UUID(claims["sid"]), world.app.state.token_settings,
        now=datetime.now(UTC) - timedelta(hours=1),
    )
    for headers, code in (({}, "authentication_required"),
                          ({"Authorization": f"Bearer {expired}"}, "invalid_token")):
        response = world.anonymous.request(method, path, json=body, headers=headers)
        assert response.status_code == 401, (method, path)
        assert response.json()["error"]["code"] == code
        assert "WWW-Authenticate" in response.headers
    upload = world.anonymous.post(
        "/api/v1/documents", files={"file": ("x.txt", b"hello", "text/plain")}
    )
    assert upload.status_code == 401
    # The document survived the anonymous DELETE.
    assert world.alice.get(f"/api/v1/documents/{world.alice_doc}").status_code == 200


def test_search_semantics_unchanged_for_an_authenticated_user(world) -> None:
    body = world.alice.post("/api/v1/search", json={"query": "lease", "top_k": 5}).json()
    assert set(body) == {"results", "metric", "embedding_model"}
    assert body["metric"] == "cosine"
    sims = [r["similarity"] for r in body["results"]]
    assert sims == sorted(sims, reverse=True)
    ask = world.alice.post("/api/v1/ask", json={"question": "lease?"}).json()
    assert ask["grounding"]["entailment_verified"] is False
    assert json.loads(world.generator.calls[-1][-1].content)["question"] == "lease?"
