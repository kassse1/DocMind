"""POST /api/v1/ask without a database: retrieval is replaced, the generator is a fake."""

import json
import logging
import uuid
from collections.abc import Callable, Iterator, Sequence
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.api.deps import get_current_user
from app.core.config import Settings, get_settings
from app.core.database import get_db_session
from app.embeddings.fake import FakeEmbedder
from app.main import create_app
from app.models import EMBEDDING_DIMENSION, User
from app.rag.context import GenerationContext, serialize_entry
from app.rag.generation import (
    ChatMessage,
    FakeGenerator,
    Generation,
    GenerationUnavailableError,
)
from app.rag.prompt import build_messages
from app.retrieval.fusion import FusedHit
from app.retrieval.vector_search import ChunkHit
from app.schemas.search import MAX_TOP_K
from app.services import ask as ask_service
from app.services import search as search_service
from app.services.search import HybridOutcome

URL = "/api/v1/ask"
USER = User(id=uuid.uuid4(), email="ask@example.com")
DOC = uuid.uuid4()


def hit(content: str, index: int = 0, page: int | None = 2, start: int = 40) -> ChunkHit:
    return ChunkHit(
        document_id=DOC,
        filename="contract.pdf",
        chunk_id=uuid.uuid5(DOC, str(index)),
        chunk_index=index,
        page_number=page,
        char_start=start,
        char_end=start + len(content),
        content=content,
        similarity=0.8,
    )


HITS = [
    hit("Invoices must be paid within 30 days of the invoice date.", 0),
    hit("A late fee of 1.5% applies per month.", 1, page=3, start=0),
]


def sources(messages: Sequence[ChatMessage]) -> list[dict[str, Any]]:
    return json.loads(messages[-1].content)["sources"]


def scripted(**reply: Any):
    return lambda _messages: json.dumps(reply)


class FakeSession:
    def __init__(self) -> None:
        self.rollbacks = 0

    def rollback(self) -> None:
        self.rollbacks += 1


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def app(settings: Settings, session: FakeSession):
    settings = settings.model_copy(
        update={"embedding_backend": "fake", "generation_backend": "fake"}
    )
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_current_user] = lambda: USER
    app.dependency_overrides[get_db_session] = lambda: session
    return app


@pytest.fixture
def api(app) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
def searches(monkeypatch) -> list[Any]:
    calls: list[Any] = []

    def fake_search(session, embedder, settings, user, request) -> list[ChunkHit]:
        calls.append(request)
        return list(HITS)

    monkeypatch.setattr(search_service, "search", fake_search)
    return calls


def use(app, responder) -> FakeGenerator:
    app.state.generator = FakeGenerator(responder)
    return app.state.generator


def test_grounded_answer_with_citation_metadata(app, api, searches, session) -> None:
    generator = use(
        app,
        scripted(
            status="answered",
            answer="Within 30 days [S1].",
            citations=[{"source_id": "S1", "quote": "paid within 30 days"}],
        ),
    )

    response = api.post(URL, json={"question": "When must invoices be paid?", "top_k": 4})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["answer"] == "Within 30 days [S1]."
    assert (body["abstained"], body["abstention_reason"]) == (False, None)
    [citation] = body["citations"]
    assert citation == {
        "source_id": "S1",
        "document_id": str(DOC),
        "filename": "contract.pdf",
        "chunk_id": str(HITS[0].chunk_id),
        "chunk_index": 0,
        "page_number": 2,
        "char_start": 40,
        "char_end": 40 + len(HITS[0].content),
        "quotes": [{"text": "paid within 30 days", "char_start": 57, "char_end": 76}],
    }
    assert body["grounding"] == {
        "policy": "quote-match-v1",
        "citation_ids_valid": True,
        "quotes_verified": True,
        "numbers_verified": True,
        "entailment_verified": False,
    }
    retrieval = body["retrieval"]
    assert (retrieval["mode"], retrieval["top_k"], retrieval["retrieved_chunks"]) == (
        "semantic", 4, 2,
    )
    assert retrieval["context_chunks"] == 2
    assert retrieval["token_counter"] == "utf8-bytes-upper-bound"
    assert body["generation_model"] == "fake/extractive"
    assert body["prompt_version"] == "grounded-qa-v1"
    assert body["timing_ms"]["generation_ms"] is not None
    # Semantic retrieval by default, with the question and top_k passed through.
    [request] = searches
    assert (request.query, request.top_k, request.document_ids) == (
        "When must invoices be paid?", 4, None,
    )
    assert [s["id"] for s in sources(generator.calls[0])] == ["S1", "S2"]
    # The read-only retrieval transaction ends before the model is called.
    assert session.rollbacks == 1


def test_default_fake_generator_answers_extractively(api, searches) -> None:
    body = api.post(URL, json={"question": "q"}).json()
    assert body["answer"] == HITS[0].content
    assert body["citations"][0]["quotes"][0]["text"] == HITS[0].content


def test_hybrid_mode_uses_hybrid_retrieval(app, api, monkeypatch) -> None:
    calls = []
    fused = FusedHit(
        **{k: v for k, v in HITS[0].__dict__.items() if k != "similarity"},
        rrf_score=0.03, vector_rank=1, vector_similarity=0.8, full_text_rank=1,
        full_text_score=0.1,
    )

    def fake_hybrid(session, embedder, settings, user, request) -> HybridOutcome:
        calls.append(request)
        return HybridOutcome(hits=[fused], candidate_pool=50, rrf_k=60)

    monkeypatch.setattr(search_service, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(search_service, "search", lambda *a: pytest.fail("semantic used"))

    body = api.post(URL, json={"question": "q", "retrieval_mode": "hybrid"}).json()

    assert body["retrieval"]["mode"] == "hybrid"
    assert body["citations"][0]["chunk_id"] == str(HITS[0].chunk_id)
    assert (calls[0].mode, calls[0].query) == ("hybrid", "q")


def test_no_retrieved_chunks_abstains_without_calling_the_model(app, api, monkeypatch) -> None:
    monkeypatch.setattr(search_service, "search", lambda *a: [])
    generator = use(app, lambda _m: pytest.fail("model called"))

    body = api.post(URL, json={"question": "q"}).json()

    assert (body["answer"], body["abstained"]) == (None, True)
    assert body["abstention_reason"] == "no_retrieved_chunks"
    assert (body["citations"], body["grounding"], body["generation_model"]) == ([], None, None)
    assert body["timing_ms"]["generation_ms"] is None
    assert generator.calls == []


def test_no_eligible_context_abstains(app, api, monkeypatch) -> None:
    monkeypatch.setattr(search_service, "search", lambda *a: [hit("   ", 0), hit("x" * 9000, 1)])
    use(app, lambda _m: pytest.fail("model called"))

    body = api.post(URL, json={"question": "q"}).json()

    assert body["abstention_reason"] == "no_eligible_context"
    assert (body["retrieval"]["excluded_empty"], body["retrieval"]["excluded_over_budget"]) == (
        1, 1,
    )


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        ("", "empty_model_output"),
        ("Sure! The answer is 30 days.", "malformed_model_output"),
        (
            json.dumps({"status": "insufficient_evidence", "answer": "", "citations": []}),
            "model_insufficient_evidence",
        ),
        (
            json.dumps(
                {
                    "status": "answered",
                    "answer": "30 days",
                    "citations": [{"source_id": "S9", "quote": "within 30 days"}],
                }
            ),
            "invalid_citation",
        ),
        (
            json.dumps({"status": "answered", "answer": "30 days", "citations": []}),
            "missing_citations",
        ),
    ],
)
def test_unusable_replies_abstain_and_hide_the_model_text(
    app, api, searches, reply: str, reason: str
) -> None:
    use(app, lambda _m: reply)
    body = api.post(URL, json={"question": "q"}).json()
    assert (body["abstained"], body["abstention_reason"], body["answer"]) == (True, reason, None)
    assert body["citations"] == []
    # The model was called, so its identity is reported even though nothing is shown.
    assert body["generation_model"] == "fake/extractive"


def test_provider_unavailable_is_503_not_an_abstention(app, api, searches) -> None:
    def down(_messages):
        raise GenerationUnavailableError("ConnectError")

    use(app, down)
    response = api.post(URL, json={"question": "q"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "generation_unavailable"


def test_generation_disabled_is_503_before_any_retrieval(settings, searches) -> None:
    settings = settings.model_copy(update={"embedding_backend": "fake"})
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    # No DB override: the request must fail before needing a session.
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(URL, json={"question": "q"})
    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "generation_unavailable",
        "message": "Answer generation is not configured on this server.",
        "details": None,
    }
    assert searches == []


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"question": ""},
        {"question": "   "},
        {"question": "x" * 1001},
        {"query": "q"},
        {"question": "q", "top_k": 0},
        {"question": "q", "top_k": MAX_TOP_K + 1},
        {"question": "q", "top_k": "5"},
        {"question": "q", "top_k": True},
        {"question": "q", "document_ids": []},
        {"question": "q", "document_ids": ["nope"]},
        {"question": "q", "retrieval_mode": "full_text"},
        {"question": "q", "model": "gpt"},
    ],
)
def test_invalid_requests_are_422(api, searches, payload: dict) -> None:
    response = api.post(URL, json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert searches == []


def test_document_ids_are_deduplicated(api, searches) -> None:
    doc = str(uuid.uuid4())
    api.post(URL, json={"question": "q", "document_ids": [doc, doc]})
    assert [str(d) for d in searches[0].document_ids] == [doc]


def test_document_not_found_is_404(api, monkeypatch) -> None:
    missing = uuid.uuid4()
    monkeypatch.setattr(search_service, "_missing_documents", lambda *_: [missing])
    response = api.post(URL, json={"question": "q", "document_ids": [str(missing)]})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "document_not_found"


class BrokenEmbedder(FakeEmbedder):
    def embed_query(self, text: str) -> np.ndarray:
        raise RuntimeError("model missing")


def test_embedding_unavailable_is_503(app, api, monkeypatch) -> None:
    # Real search service path up to the embedding call (no document filter, no DB use).
    app.state.embedder = BrokenEmbedder(EMBEDDING_DIMENSION)
    response = api.post(URL, json={"question": "q"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "embedding_unavailable"


def test_database_failure_is_503(api, monkeypatch) -> None:
    def failing(*_a):
        raise OperationalError("SELECT", {}, Exception("canceling statement due to timeout"))

    monkeypatch.setattr(search_service, "search", failing)
    response = api.post(URL, json={"question": "q"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "database_unavailable"


def test_prod_requires_authentication(searches) -> None:
    settings = Settings(_env_file=None, environment="prod", generation_backend="openai-compatible")
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_db_session] = lambda: FakeSession()
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(URL, json={"question": "q"})
    assert response.status_code == 401
    assert searches == []


def test_question_and_passages_are_never_logged(app, api, monkeypatch, caplog) -> None:
    secret_hit = hit("SECRET-PASSAGE text with enough words.", 0)
    monkeypatch.setattr(search_service, "search", lambda *a: [secret_hit])
    caplog.set_level(logging.DEBUG)

    for responder in (
        scripted(status="answered", answer="SECRET-ANSWER", citations=[
            {"source_id": "S1", "quote": "SECRET-PASSAGE text"}
        ]),
        lambda _m: "SECRET-GARBAGE",
    ):
        use(app, responder)
        assert api.post(URL, json={"question": "SECRET-QUESTION"}).status_code == 200

    for leaked in ("SECRET-QUESTION", "SECRET-PASSAGE", "SECRET-ANSWER", "SECRET-GARBAGE"):
        assert all(leaked not in record.getMessage() for record in caplog.records)
    assert any("Ask: user=" in record.getMessage() for record in caplog.records)


class ExactWordCounter:
    """Stands in for the model tokenizer: one token per word, counts declared exact."""

    name = "test-words"
    exact = True

    def count(self, text: str) -> int:
        return len(text.split())


class ReportingGenerator(FakeGenerator):
    """Reports a server-side prompt size, like Ollama's usage.prompt_tokens."""

    def __init__(self, prompt_tokens: Callable[[int], int]) -> None:
        super().__init__()
        self._prompt_tokens = prompt_tokens

    def generate(self, messages: Sequence[ChatMessage]) -> Generation:
        reply = super().generate(messages)
        sent = sum(ExactWordCounter().count(m.content) for m in messages)
        return Generation(reply.text, self._prompt_tokens(sent))


def _windowed(app, settings: Settings, **update: Any) -> None:
    settings = settings.model_copy(
        update={"embedding_backend": "fake", "generation_backend": "fake", **update}
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.token_counter = ExactWordCounter()


def test_context_window_caps_the_passage_budget(app, api, settings, monkeypatch) -> None:
    passages = [hit(" ".join(["word"] * 100), i) for i in range(5)]
    monkeypatch.setattr(search_service, "search", lambda *a: passages)
    fixed = sum(
        ExactWordCounter().count(m.content)
        for m in build_messages("q", GenerationContext((), 1, "", 0, 0))
    )
    text = " ".join(["word"] * 100)
    entry = ExactWordCounter().count(
        serialize_entry({"id": "S1", "filename": "contract.pdf", "page": 2, "text": text})
    )
    # Room for exactly two passages after the reply, the instructions and the reserve.
    window = 100 + fixed + ask_service.TEMPLATE_RESERVE_TOKENS + 2 * entry + entry // 2
    _windowed(app, settings, generation_context_window=window, generation_max_output_tokens=100)

    body = api.post(URL, json={"question": "q"}).json()

    retrieval = body["retrieval"]
    assert retrieval["context_token_budget"] == 2 * entry + entry // 2
    assert (retrieval["context_chunks"], retrieval["excluded_over_budget"]) == (2, 3)
    assert retrieval["context_tokens"] == 2 * entry


def test_configured_budget_applies_when_the_window_is_large(app, api, settings, searches):
    _windowed(app, settings, generation_context_window=100_000, rag_context_token_budget=500)
    assert api.post(URL, json={"question": "q"}).json()["retrieval"]["context_token_budget"] == 500


def test_prompt_too_long_for_the_window_abstains_without_calling_the_model(
    app, api, settings, searches
) -> None:
    # The instructions alone (a few hundred words) exceed what the reply leaves free.
    _windowed(app, settings, generation_context_window=512, generation_max_output_tokens=400)
    generator = use(app, lambda _m: pytest.fail("model called"))

    body = api.post(URL, json={"question": "q"}).json()

    assert body["abstention_reason"] == "no_eligible_context"
    assert body["retrieval"]["context_token_budget"] < 1
    assert body["retrieval"]["excluded_over_budget"] == len(HITS)
    assert generator.calls == []


def test_prompt_truncated_by_the_server_is_503(app, api, settings, searches, caplog) -> None:
    _windowed(app, settings)
    # Ollama keeps only the end of an oversized prompt and still answers 200.
    app.state.generator = ReportingGenerator(lambda sent: sent // 2)

    response = api.post(URL, json={"question": "SECRET-QUESTION"})

    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "generation_unavailable",
        "message": "The model server truncated the prompt. Try again later.",
        "details": None,
    }
    assert any("truncated" in r.getMessage() for r in caplog.records)
    assert all("SECRET-QUESTION" not in r.getMessage() for r in caplog.records)


def test_full_prompt_reported_by_the_server_is_accepted(app, api, settings, searches) -> None:
    _windowed(app, settings)
    # The server counts the chat template on top of the content: more is fine.
    app.state.generator = ReportingGenerator(lambda sent: sent + 13)
    body = api.post(URL, json={"question": "q"}).json()
    assert body["abstained"] is False


def test_truncation_is_not_checked_with_the_byte_upper_bound(app, api, searches) -> None:
    # Bytes always exceed tokens, so a smaller server count proves nothing.
    app.state.generator = ReportingGenerator(lambda sent: 1)
    assert api.post(URL, json={"question": "q"}).status_code == 200
