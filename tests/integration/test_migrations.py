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

APP_TABLES = {
    "users", "documents", "document_chunks", "conversations", "messages", "jobs",
    "auth_sessions", "refresh_tokens",
}
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


PHASE4_REVISION = "76cdab7f68eb"


def test_full_text_upgrade_backfills_existing_chunks(pg_engine: Engine) -> None:
    """Chunks indexed before Phase 6 become searchable without re-upload or re-index."""
    run_alembic(pg_engine, command.downgrade, PHASE4_REVISION)
    user_id, doc_id = uuid.uuid4(), uuid.uuid4()
    vector = "[" + ",".join(["0.05"] * 384) + "]"
    try:
        with pg_engine.begin() as conn:
            conn.execute(
                text("INSERT INTO users (id, email) VALUES (:id, 'fts-mig@example.com')"),
                {"id": user_id},
            )
            conn.execute(
                text(
                    "INSERT INTO documents (id, user_id, filename, content_type, file_size,"
                    " content_hash, status) VALUES (:id, :uid, 'f.txt', 'text/plain', 1,"
                    " :hash, 'ready')"
                ),
                {"id": doc_id, "uid": user_id, "hash": "a" * 64},
            )
            conn.execute(
                text(
                    "INSERT INTO document_chunks (id, document_id, chunk_index, content,"
                    " char_start, char_end, token_count, embedding, embedding_model)"
                    " VALUES (gen_random_uuid(), :doc, 0, 'Счета оплачиваются в течение"
                    " десяти дней', 0, 37, 6, CAST(:v AS vector), 'm')"
                ),
                {"doc": doc_id, "v": vector},
            )
        run_alembic(pg_engine, command.upgrade, "head")
        with pg_engine.connect() as conn:
            matches = conn.scalar(
                text(
                    "SELECT count(*) FROM document_chunks WHERE document_id = :doc AND "
                    "search_vector @@ plainto_tsquery('russian'::regconfig, 'счёт')"
                ),
                {"doc": doc_id},
            )
            index = conn.scalar(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE indexname = 'ix_document_chunks_search_vector'"
                )
            )
        assert matches == 1
        assert "USING gin (search_vector)" in index
    finally:
        run_alembic(pg_engine, command.upgrade, "head")
        with pg_engine.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})


def test_full_text_downgrade_removes_column_and_index(pg_engine: Engine) -> None:
    run_alembic(pg_engine, command.downgrade, PHASE4_REVISION)
    try:
        columns = {c["name"] for c in inspect(pg_engine).get_columns("document_chunks")}
        indexes = {i["name"] for i in inspect(pg_engine).get_indexes("document_chunks")}
        assert "search_vector" not in columns
        assert "ix_document_chunks_search_vector" not in indexes
    finally:
        run_alembic(pg_engine, command.upgrade, "head")


PHASE7_REVISION = "128b8af81b05"


def test_auth_upgrade_keeps_legacy_users_and_documents_without_passwords(pg_engine) -> None:
    run_alembic(pg_engine, command.downgrade, PHASE7_REVISION)
    user_id, document_id = uuid.uuid4(), uuid.uuid4()
    try:
        with pg_engine.begin() as conn:
            conn.execute(text("INSERT INTO users (id, email) VALUES (:u, 'legacy@example.com')"),
                         {"u": user_id})
            conn.execute(
                text(
                    "INSERT INTO documents (id, user_id, filename, content_type, file_size, "
                    "content_hash) VALUES (:d, :u, 'a.txt', 'text/plain', 1, :h)"
                ),
                {"d": document_id, "u": user_id, "h": "a" * 64},
            )
        run_alembic(pg_engine, command.upgrade, "head")
        with pg_engine.connect() as conn:
            row = conn.execute(
                text("SELECT email, password_hash, is_active FROM users WHERE id = :u"),
                {"u": user_id},
            ).one()
            owner = conn.scalar(text("SELECT user_id FROM documents WHERE id = :d"),
                                {"d": document_id})
        # Same account, same owner, no password: it cannot log in until an operator sets one.
        assert tuple(row) == ("legacy@example.com", None, True)
        assert owner == user_id
    finally:
        run_alembic(pg_engine, command.upgrade, "head")
        with pg_engine.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})


def test_auth_upgrade_refuses_unnormalized_emails(pg_engine) -> None:
    run_alembic(pg_engine, command.downgrade, PHASE7_REVISION)
    try:
        with pg_engine.begin() as conn:
            conn.execute(text("INSERT INTO users (id, email) VALUES (:u, 'Mixed@Example.com')"),
                         {"u": uuid.uuid4()})
        with pytest.raises(RuntimeError, match="not normalized"):
            run_alembic(pg_engine, command.upgrade, "head")
    finally:
        with pg_engine.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE email = 'Mixed@Example.com'"))
        run_alembic(pg_engine, command.upgrade, "head")


def test_database_rejects_unnormalized_email_after_upgrade(pg_engine) -> None:
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError), pg_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES (:u, ' A@Example.com')"),
                     {"u": uuid.uuid4()})
