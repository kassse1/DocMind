import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.v1.router import api_router
from app.core.body_limit import BodySizeLimitMiddleware
from app.core.config import Settings, get_settings
from app.core.database import create_db_engine, create_session_factory
from app.core.errors import register_exception_handlers
from app.ingestion.storage import FileStorage


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    # Close pooled connections on shutdown. Schema is managed by Alembic, never create_all().
    app.state.engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    """App factory: lets tests build isolated apps with custom settings."""
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level)

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        debug=settings.debug,
        lifespan=lifespan,
    )
    # Engine creation is lazy (no connection yet), so the app starts even if the DB is down.
    app.state.engine = create_db_engine(settings)
    app.state.session_factory = create_session_factory(app.state.engine)
    app.state.storage = FileStorage(settings.storage_dir)

    app.add_middleware(BodySizeLimitMiddleware, max_upload_bytes=settings.max_upload_bytes)
    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.api_v1_prefix)
    return app


app = create_app()
