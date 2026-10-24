import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, inspect, text

from app.models import Base
from tests.integration.conftest import run_alembic

pytestmark = pytest.mark.integration

APP_TABLES = {"users", "documents", "document_chunks", "conversations", "messages"}


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


def test_pgvector_not_enabled_yet(pg_engine: Engine) -> None:
    with pg_engine.connect() as conn:
        installed = conn.scalar(text("SELECT count(*) FROM pg_extension WHERE extname = 'vector'"))
    assert installed == 0
