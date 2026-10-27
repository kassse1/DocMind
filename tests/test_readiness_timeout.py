"""Readiness against a database that accepts TCP connections but never answers
(Phase 12 failure drill finding: with PostgreSQL paused, /health/ready did not answer
within 30 s because the check used a pooled connection whose pre-ping blocked forever)."""

import socket
import threading
import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.core.database import _probe_engine, ping_database
from app.main import create_app


@pytest.fixture
def silent_server() -> Iterator[int]:
    """Accepts connections and reads nothing back: a frozen server, seen from the client."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    accepted: list[socket.socket] = []
    stop = threading.Event()

    def serve() -> None:
        listener.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
                accepted.append(conn)
            except OSError:
                continue

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield listener.getsockname()[1]
    stop.set()
    thread.join(2)
    for conn in accepted:
        conn.close()
    listener.close()


def _url(port: int) -> str:
    return f"postgresql+psycopg://docmind:pw@127.0.0.1:{port}/docmind"


def test_ping_gives_up_quickly_on_a_server_that_never_answers(silent_server: int) -> None:
    engine = create_engine(_url(silent_server), connect_args={"connect_timeout": 30})
    started = time.monotonic()
    with pytest.raises(OperationalError):
        ping_database(engine, timeout_ms=1000)
    # Bounded by the readiness timeout, not by the engine's own (30 s) connect timeout.
    assert time.monotonic() - started < 5


def test_readiness_answers_503_promptly_when_the_database_hangs(
    settings: Settings, silent_server: int
) -> None:
    hung = settings.model_copy(update={
        "database_url": SecretStr(_url(silent_server)), "db_readiness_timeout_ms": 1000,
    })
    with TestClient(create_app(hung), raise_server_exceptions=False) as client:
        started = time.monotonic()
        response = client.get("/api/v1/health/ready")
        elapsed = time.monotonic() - started
    assert response.status_code == 503
    assert response.json()["error"]["details"] == {"checks": {"database": "unavailable"}}
    assert elapsed < 5


def test_the_probe_never_uses_the_request_pool() -> None:
    engine = create_engine(_url(1), pool_size=2)
    probe = _probe_engine(engine, 2)
    assert isinstance(probe.pool, NullPool)
    assert probe.url == engine.url
    # One probe engine per application engine.
    assert _probe_engine(engine, 2) is probe
