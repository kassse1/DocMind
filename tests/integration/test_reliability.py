"""Readiness against the real schema, the worker's schema guard and heartbeat, and the
periodic cleanup. (Stale-job recovery is covered in test_indexing.py.)"""

import os
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.schema import expected_head
from app.main import create_app
from app.services import maintenance
from app.worker.__main__ import SCHEMA_MISMATCH_EXIT
from app.worker.__main__ import main as worker_main
from tests.integration.conftest import run_alembic

pytestmark = pytest.mark.integration


@pytest.fixture
def worker_env(test_database_url: str, tmp_path: Path, monkeypatch) -> Iterator[Path]:
    heartbeat = tmp_path / "heartbeat"
    monkeypatch.setenv("DOCMIND_DATABASE_URL", test_database_url)
    monkeypatch.setenv("DOCMIND_EMBEDDING_BACKEND", "fake")
    monkeypatch.setenv("DOCMIND_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("DOCMIND_WORKER_HEARTBEAT_FILE", str(heartbeat))
    get_settings.cache_clear()
    yield heartbeat
    get_settings.cache_clear()


def test_readiness_requires_the_current_schema(test_database_url, pg_engine) -> None:
    app = create_app(Settings(_env_file=None, environment="test", database_url=test_database_url))
    with TestClient(app, raise_server_exceptions=False) as client:
        ready = client.get("/api/v1/health/ready")
        assert ready.status_code == 200
        assert ready.json()["checks"] == {"database": "ok", "schema": "ok"}

        run_alembic(pg_engine, command.downgrade, "-1")
        try:
            outdated = client.get("/api/v1/health/ready")
            live = client.get("/api/v1/health")
        finally:
            run_alembic(pg_engine, command.upgrade, "head")
    assert outdated.status_code == 503
    assert outdated.json()["error"]["details"]["checks"]["schema"] == "outdated"
    # Liveness never depends on the database or its schema.
    assert live.status_code == 200


def test_worker_refuses_an_outdated_schema(worker_env, pg_engine) -> None:
    run_alembic(pg_engine, command.downgrade, "-1")
    try:
        assert worker_main(["--once"]) == SCHEMA_MISMATCH_EXIT
    finally:
        run_alembic(pg_engine, command.upgrade, "head")
    assert not worker_env.exists()  # it never got as far as starting


def test_worker_runs_and_heartbeat_healthcheck(worker_env) -> None:
    assert worker_main(["healthcheck"]) == 1  # no heartbeat yet
    assert worker_main(["--once"]) == 0
    assert worker_env.exists()
    assert worker_main(["healthcheck", "--max-age", "60"]) == 0
    old = time.time() - 600
    os.utime(worker_env, (old, old))
    assert worker_main(["healthcheck", "--max-age", "60"]) == 1


def test_expected_head_matches_the_migrated_database(pg_engine) -> None:
    with pg_engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == expected_head()


def test_maintenance_removes_only_old_rows(pg_engine) -> None:
    user_id = uuid.uuid4()
    with pg_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES (:u, 'm@example.com')"),
                     {"u": user_id})
        rows = {
            "live": "now() + interval '1 day', NULL",
            "expired_old": "now() - interval '8 days', NULL",
            "revoked_old": "now() + interval '1 day', now() - interval '8 days'",
            "revoked_recent": "now() + interval '1 day', now() - interval '1 day'",
        }
        ids = {}
        for name, values in rows.items():
            ids[name] = uuid.uuid4()
            conn.execute(text(
                f"INSERT INTO auth_sessions (id, user_id, expires_at, revoked_at) "
                f"VALUES (:id, :u, {values})"
            ), {"id": ids[name], "u": user_id})
        for name, expires in (("old_token", "now() - interval '2 days'"),
                              ("fresh_token", "now() + interval '1 day'")):
            conn.execute(text(
                f"INSERT INTO refresh_tokens (id, session_id, token_hash, expires_at) "
                f"VALUES (gen_random_uuid(), :s, :h, {expires})"
            ), {"s": ids["live"], "h": name.ljust(64, "x")})
        conn.execute(text(
            "INSERT INTO rate_limits (key, window_start, count) VALUES "
            "(repeat('a', 64), now() - interval '2 days', 3), (repeat('b', 64), now(), 1)"
        ))
    try:
        with Session(pg_engine) as session:
            removed = maintenance.prune(session)
        assert removed == {"rate_limits": 1, "refresh_tokens": 1, "auth_sessions": 2}
        with pg_engine.connect() as conn:
            left = set(conn.scalars(text("SELECT id FROM auth_sessions")))
        assert left == {ids["live"], ids["revoked_recent"]}
    finally:
        with pg_engine.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})
            conn.execute(text("TRUNCATE rate_limits"))
