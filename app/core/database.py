"""Database engine, session factory and FastAPI dependencies (sync SQLAlchemy 2.x).

The engine is created once per app in create_app() and disposed on shutdown.
create_engine() is lazy: no connection is opened until the first query, so the app
starts (and /health answers) even when PostgreSQL is down.
"""

from collections.abc import Iterator

from fastapi import Request
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings


def create_db_engine(settings: Settings) -> Engine:
    return create_engine(
        settings.database_url.get_secret_value(),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_s,
        # Detect connections dropped by a DB restart before handing them to a request.
        pool_pre_ping=True,
        connect_args={"connect_timeout": settings.db_connect_timeout_s},
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    # expire_on_commit=False: objects stay readable after commit (e.g. to build a response).
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_engine(request: Request) -> Engine:
    return request.app.state.engine


def get_db_session(request: Request) -> Iterator[Session]:
    """One session per request. Services commit explicitly; anything not committed
    is rolled back, and the connection always returns to the pool."""
    session: Session = request.app.state.session_factory()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def ping_database(engine: Engine, timeout_ms: int) -> None:
    """Run a trivial query with a server-side timeout. Raises on any failure."""
    with engine.connect() as conn:
        # set_config(..., true) is transaction-local, so the timeout never leaks to pooled reuse.
        conn.execute(
            text("SELECT set_config('statement_timeout', :t, true)"), {"t": str(timeout_ms)}
        )
        conn.execute(text("SELECT 1"))
