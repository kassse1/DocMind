"""Alembic environment: URL from app settings, metadata from app.models."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, create_engine, pool

from app.core.config import get_settings
from app.models import Base

config = context.config
target_metadata = Base.metadata

# Tests pass an open connection via config.attributes; they manage logging themselves.
_external_connection: Connection | None = config.attributes.get("connection")

if config.config_file_name is not None and _external_connection is None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def _configure(**kwargs: object) -> None:
    context.configure(
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        **kwargs,
    )


def run_migrations_offline() -> None:
    """`alembic upgrade head --sql`: emit SQL without connecting."""
    _configure(
        url=get_settings().database_url.get_secret_value(),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_with_connection(connection: Connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    if _external_connection is not None:
        _run_with_connection(_external_connection)
        return

    engine = create_engine(get_settings().database_url.get_secret_value(), poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            _run_with_connection(connection)
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
