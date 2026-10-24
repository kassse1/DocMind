"""The real app wired to the real test database."""

from collections.abc import Iterator
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import get_db_session
from app.main import create_app
from app.models import User

pytestmark = pytest.mark.integration


@pytest.fixture
def app(test_database_url: str, pg_engine: Engine) -> FastAPI:
    settings = Settings(_env_file=None, environment="test", database_url=test_database_url)
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings

    @app.post("/_test/uncommitted")
    def uncommitted(session: Annotated[Session, Depends(get_db_session)]) -> dict[str, str]:
        session.add(User(email="uncommitted@example.com"))
        session.flush()
        return {"status": "flushed"}

    @app.post("/_test/fails")
    def fails(session: Annotated[Session, Depends(get_db_session)]) -> None:
        session.add(User(email="failed@example.com"))
        session.flush()
        raise RuntimeError("boom")

    return app


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _user_count(engine: Engine) -> int:
    with engine.connect() as conn:
        return conn.scalar(select(func.count()).select_from(User))


def test_readiness_ok_with_real_database(client: TestClient) -> None:
    response = client.get("/api/v1/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"database": "ok"}}


def test_request_session_never_auto_commits(client: TestClient, pg_engine: Engine) -> None:
    before = _user_count(pg_engine)

    assert client.post("/_test/uncommitted").status_code == 200
    assert client.post("/_test/fails").status_code == 500

    assert _user_count(pg_engine) == before
