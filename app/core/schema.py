"""Database schema version check against the Alembic head shipped with the code.

The API reports not-ready and the worker refuses to start while the database is not at
the head revision, so neither runs against an older (or newer) schema after a deployment.
Migrations themselves are never run from here: a one-shot migrate step does that
(docker compose "migrate" service or `alembic upgrade head`), never every process.
"""

from functools import lru_cache
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, text
from sqlalchemy.exc import ProgrammingError

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@lru_cache
def expected_head() -> str:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    head = ScriptDirectory.from_config(config).get_current_head()
    if head is None:
        raise RuntimeError("No Alembic revisions found")
    return head


def current_revision(conn: Connection) -> str | None:
    """None when the database was never migrated (no alembic_version table)."""
    try:
        with conn.begin_nested():
            return conn.scalar(text("SELECT version_num FROM alembic_version"))
    except ProgrammingError:
        return None


def schema_is_current(conn: Connection) -> bool:
    return current_revision(conn) == expected_head()
