"""Database engine, session factory and FastAPI dependencies (sync SQLAlchemy 2.x).

The engine is created once per app in create_app() and disposed on shutdown.
create_engine() is lazy: no connection is opened until the first query, so the app
starts (and /health answers) even when PostgreSQL is down.
"""

import math
from collections.abc import Iterator
from weakref import WeakKeyDictionary

from fastapi import Request
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

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


_probe_engines: "WeakKeyDictionary[Engine, Engine]" = WeakKeyDictionary()


def _probe_engine(engine: Engine, connect_timeout_s: int) -> Engine:
    """Same database, no pool: every readiness check opens a fresh connection.

    A pooled connection to a server that hangs (frozen process, partition) blocks in the
    pre-ping read forever: the server-side statement_timeout never fires and the client
    connect_timeout only covers new connections. A fresh connection is bounded by
    connect_timeout (it includes waiting for the server's startup reply), so readiness
    answers 503 within seconds instead of hanging a worker thread.
    """
    probe = _probe_engines.get(engine)
    if probe is None:
        probe = create_engine(
            engine.url, poolclass=NullPool, connect_args={"connect_timeout": connect_timeout_s}
        )
        _probe_engines[engine] = probe
    return probe


def ping_database(engine: Engine, timeout_ms: int) -> bool:
    """Run trivial queries on a fresh connection, bounded by timeout_ms both for connecting
    and (server side) for the queries; True when the schema is at the Alembic head of this
    code. Raises on any connection failure."""
    from app.core.schema import schema_is_current

    probe = _probe_engine(engine, max(1, math.ceil(timeout_ms / 1000)))
    with probe.connect() as conn:
        # set_config(..., true) is transaction-local (the connection is not reused anyway).
        conn.execute(
            text("SELECT set_config('statement_timeout', :t, true)"), {"t": str(timeout_ms)}
        )
        conn.execute(text("SELECT 1"))
        return schema_is_current(conn)
