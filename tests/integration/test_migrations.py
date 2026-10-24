import hashlib
import uuid

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, inspect, text

from app.models import Base
from tests.integration.conftest import run_alembic

pytestmark = pytest.mark.integration

APP_TABLES = {"users", "documents", "document_chunks", "conversations", "messages", "jobs"}
INITIAL_REVISION = "fead9350b593"


def _tables(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def test_downgrade_to_base_then_upgrade_rebuilds_schema(pg_engine: Engine) -> None:
    run_alembic(pg_engine, command.downgrade, "base")
    assert _tables(pg_engine) == {"alembic_version"}

    run_alembic(pg_engine, command.upgrade, "head")
    assert _tables(pg_engine) == APP_TABLES | {"alembic_version"}


def test_migrations_match_models(pg_engine: Engine) -> None:
    """Catches drift: a model change that was never captured in a migration."""
    with pg_engine.connect() as conn:
        ctx = MigrationContext.configure(
            conn, opts={"compare_type": True, "compare_server_default": True}
        )
        assert compare_metadata(ctx, Base.metadata) == []


def _vector_installed(engine: Engine) -> bool:
    with engine.connect() as conn:
        return bool(conn.scalar(text("SELECT count(*) FROM pg_extension WHERE extname = 'vector'")))


def test_pgvector_enabled_at_head_and_removed_on_downgrade(pg_engine: Engine) -> None:
    assert _vector_installed(pg_engine)
    with pg_engine.connect() as conn:
        column_type = conn.scalar(
            text(
                "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                "WHERE attrelid = 'document_chunks'::regclass AND attname = 'embedding'"
            )
        )
    assert column_type == "vector(384)"

    run_alembic(pg_engine, command.downgrade, INITIAL_REVISION)
    try:
        assert not _vector_installed(pg_engine)
        assert "jobs" not in _tables(pg_engine)
    finally:
        run_alembic(pg_engine, command.upgrade, "head")


def test_upgrade_enqueues_jobs_for_pending_documents(pg_engine: Engine) -> None:
    """Documents uploaded before Phase 4 must get indexed after the upgrade."""
    run_alembic(pg_engine, command.downgrade, INITIAL_REVISION)
    user_id = uuid.uuid4()
    doc_ids = {status: uuid.uuid4() for status in ("pending", "failed")}
    try:
        with pg_engine.begin() as conn:
            conn.execute(
                text("INSERT INTO users (id, email) VALUES (:id, 'm@example.com')"),
                {"id": user_id},
            )
            for status, doc_id in doc_ids.items():
                conn.execute(
                    text(
                        "INSERT INTO documents (id, user_id, filename, content_type, file_size,"
                        " content_hash, status) VALUES (:id, :uid, 'f.txt', 'text/plain', 1,"
                        " :hash, :status)"
                    ),
                    {"id": doc_id, "uid": user_id, "status": status,
                     "hash": hashlib.sha256(status.encode()).hexdigest()},
                )
        run_alembic(pg_engine, command.upgrade, "head")
        with pg_engine.connect() as conn:
            jobs = conn.execute(text("SELECT document_id, kind, status FROM jobs")).all()
        assert jobs == [(doc_ids["pending"], "index_document", "queued")]
    finally:
        run_alembic(pg_engine, command.upgrade, "head")
        with pg_engine.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
