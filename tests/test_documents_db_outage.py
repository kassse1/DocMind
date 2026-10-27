"""Documents endpoints during a database outage (Phase 12 failure drill finding).

With PostgreSQL stopped, GET /documents answered 500 internal_error: the router was not
using DatabaseUnavailableRoute like auth, search and ask. It must answer 503
database_unavailable without leaking connection details.
"""

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from app.api.deps import get_current_user
from app.core.config import Settings, get_settings
from app.main import create_app
from app.models import User

DOC = str(uuid.uuid4())
REQUESTS = [
    ("GET", "/api/v1/documents"),
    ("GET", f"/api/v1/documents/{DOC}"),
    ("DELETE", f"/api/v1/documents/{DOC}"),
    ("POST", "/api/v1/documents"),
]


def _secret_error() -> OperationalError:
    return OperationalError(
        "SELECT ...", {}, Exception("connection to 10.0.0.5 failed for user=admin SECRETPW")
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[tuple[object, TestClient]]:
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield app, test_client


@pytest.mark.parametrize(("method", "path"), REQUESTS, ids=[f"{m} {p}" for m, p in REQUESTS])
@pytest.mark.parametrize(
    "error", [_secret_error(), PoolTimeoutError("QueuePool limit reached")],
    ids=["operational", "pool-timeout"],
)
def test_database_failure_is_503_without_details(client, caplog, method, path, error) -> None:
    app, api = client

    def failing_user() -> User:
        raise error

    app.dependency_overrides[get_current_user] = failing_user
    files = {"file": ("a.txt", b"hello", "text/plain")} if method == "POST" else None

    response = api.request(method, path, files=files)

    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "database_unavailable",
        "message": "The database is unavailable. Try again later.",
        "details": None,
    }
    for leaked in ("SECRETPW", "10.0.0.5", "QueuePool"):
        assert leaked not in response.text
        assert all(leaked not in r.getMessage() for r in caplog.records)
