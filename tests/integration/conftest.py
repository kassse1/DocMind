"""PostgreSQL integration fixtures.

Set DOCMIND_TEST_DATABASE_URL to a dedicated database whose name ends in "_test".
The schema is wiped and rebuilt with Alembic (not create_all) once per test session;
each test then runs inside a transaction that is rolled back, so tests never see
each other's rows.
"""

import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEST_DB_ENV = "DOCMIND_TEST_DATABASE_URL"


def alembic_config(connection: Connection) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.attributes["connection"] = connection
    return cfg


def run_alembic(engine: Engine, action: Callable[[Config, str], None], revision: str) -> None:
    with engine.begin() as conn:
        action(alembic_config(conn), revision)


@pytest.fixture(scope="session")
def test_database_url() -> str:
    raw = os.environ.get(TEST_DB_ENV)
    if not raw:
        pytest.skip(f"{TEST_DB_ENV} is not set; skipping PostgreSQL integration tests")
    database = make_url(raw).database or ""
    if not database.endswith("_test"):
        # The fixture drops the whole schema: never point it at a real database.
        pytest.fail(f"Refusing to run: test database name must end with '_test' ({database!r})")
    return raw


@pytest.fixture(scope="session")
def pg_engine(test_database_url: str) -> Iterator[Engine]:
    engine = create_engine(test_database_url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    run_alembic(engine, command.upgrade, "head")
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(pg_engine: Engine) -> Iterator[Session]:
    with pg_engine.connect() as conn:
        outer = conn.begin()
        # Session work happens in a SAVEPOINT, so a test can hit an IntegrityError,
        # roll back, and keep going; the outer transaction is always rolled back.
        session = Session(bind=conn, join_transaction_mode="create_savepoint")
        try:
            yield session
        finally:
            session.close()
            outer.rollback()


def expect_violation(session: Session, constraint: str, action: Callable[[], object]) -> None:
    """Assert that `action` fails on the named constraint, then recover the session."""
    with pytest.raises(IntegrityError) as exc_info:
        action()
    session.rollback()
    assert exc_info.value.orig.diag.constraint_name == constraint


TEST_PASSWORD = "integration-test-passphrase"
_password_hash: str | None = None


def authenticate(client, email: str):
    """Give a TestClient a real access token for `email` (created if needed, with a real
    Argon2id hash) and a real login session, signed with the app's own key."""
    from datetime import UTC, datetime, timedelta

    from app.core.security import create_access_token, hash_password
    from app.models import AuthSession
    from app.services.users import get_or_create_user

    global _password_hash
    if _password_hash is None:
        _password_hash = hash_password(TEST_PASSWORD)
    app = client.app
    with app.state.session_factory() as session:
        user = get_or_create_user(session, email)
        if user.password_hash is None:
            user.password_hash = _password_hash
            session.commit()
        auth_session = AuthSession(
            user_id=user.id, expires_at=datetime.now(UTC) + timedelta(days=1)
        )
        session.add(auth_session)
        session.commit()
        user_id, session_id = user.id, auth_session.id
    token = create_access_token(user_id, session_id, app.state.token_settings)
    client.headers["Authorization"] = f"Bearer {token}"
    return client


@pytest.fixture(autouse=True)
def _reset_rate_limits(request) -> Iterator[None]:
    """Rate-limit counters live in PostgreSQL and outlive TRUNCATE users: start each
    integration test from zero so limits never leak between tests."""
    engine = request.getfixturevalue("pg_engine")
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE rate_limits"))
    yield
