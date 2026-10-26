"""Database wiring tests that need no running PostgreSQL."""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.api.v1 import health as health_module
from app.core.config import Settings, get_settings
from app.core.database import get_db_session
from app.main import create_app
from app.models import EMBEDDING_DIMENSION, Base

# Nothing listens on port 1, so connecting fails fast with "connection refused".
UNREACHABLE_URL = "postgresql+psycopg://docmind:SUPER-SECRET-PW@127.0.0.1:1/docmind"


def _client(settings: Settings) -> TestClient:
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app, raise_server_exceptions=False)


def test_database_url_from_env_and_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCMIND_DATABASE_URL", UNREACHABLE_URL)

    settings = Settings(_env_file=None)

    assert settings.database_url.get_secret_value() == UNREACHABLE_URL
    assert "SUPER-SECRET-PW" not in repr(settings)
    assert "SUPER-SECRET-PW" not in str(settings.model_dump())


def test_liveness_does_not_touch_database() -> None:
    settings = Settings(_env_file=None, environment="test", database_url=UNREACHABLE_URL)

    with _client(settings) as c:
        response = c.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_readiness_unreachable_db_returns_safe_503() -> None:
    settings = Settings(
        _env_file=None, environment="test", database_url=UNREACHABLE_URL, db_connect_timeout_s=1
    )

    with _client(settings) as c:
        response = c.get("/api/v1/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "service_unavailable",
            "message": "Service is not ready",
            "details": {"checks": {"database": "unavailable"}},
        }
    }
    for leaked in ("SUPER-SECRET-PW", "docmind", "127.0.0.1", "psycopg", "OperationalError"):
        assert leaked not in response.text


def test_readiness_ok_when_ping_succeeds(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(health_module, "ping_database", lambda _engine, ms: calls.append(ms))

    with _client(settings) as c:
        response = c.get("/api/v1/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"database": "ok"}}
    assert calls == [settings.db_readiness_timeout_ms]


def _request_with_session(session: MagicMock) -> MagicMock:
    request = MagicMock()
    request.app.state.session_factory.return_value = session
    return request


def test_session_dependency_closes_without_committing() -> None:
    session = MagicMock()
    gen = get_db_session(_request_with_session(session))

    assert next(gen) is session
    with pytest.raises(StopIteration):
        next(gen)

    session.close.assert_called_once()
    session.commit.assert_not_called()
    session.rollback.assert_not_called()


def test_session_dependency_rolls_back_on_error() -> None:
    session = MagicMock()
    gen = get_db_session(_request_with_session(session))
    next(gen)

    with pytest.raises(RuntimeError):
        gen.throw(RuntimeError("handler failed"))

    session.rollback.assert_called_once()
    session.close.assert_called_once()


def test_all_models_registered_on_metadata() -> None:
    assert set(Base.metadata.tables) == {
        "users",
        "documents",
        "document_chunks",
        "conversations",
        "messages",
        "jobs",
        "auth_sessions",
        "refresh_tokens",
        "rate_limits",
    }


def test_embedding_column_has_fixed_dimension() -> None:
    column = Base.metadata.tables["document_chunks"].columns["embedding"]
    assert column.type.dim == EMBEDDING_DIMENSION
    assert not column.nullable
