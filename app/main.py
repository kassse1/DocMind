import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from app.api.v1.router import api_router
from app.core.body_limit import BodySizeLimitMiddleware
from app.core.config import Settings, get_settings
from app.core.database import create_db_engine, create_session_factory
from app.core.errors import register_exception_handlers
from app.core.http_security import SecurityHeadersMiddleware
from app.core.security import TokenSettings
from app.embeddings.factory import create_embedder
from app.ingestion.storage import FileStorage
from app.models import EMBEDDING_DIMENSION
from app.rag.generation import create_generator
from app.rag.tokens import create_token_counter


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    # Close pooled connections on shutdown. Schema is managed by Alembic, never create_all().
    app.state.engine.dispose()
    if app.state.generator is not None:
        app.state.generator.close()


def _token_settings(settings: Settings) -> TokenSettings:
    if settings.jwt_secret_key is not None:
        secret = settings.jwt_secret_key.get_secret_value()
    else:
        # dev/test only (Settings refuses prod without a secret): a random key per process.
        # Never a fixed default, so a forgotten configuration cannot produce forgeable tokens.
        secret = secrets.token_urlsafe(48)
        logging.getLogger(__name__).warning(
            "DOCMIND_JWT_SECRET_KEY is not set: using a random per-process signing key; "
            "tokens become invalid when the process restarts"
        )
    return TokenSettings(
        secret=secret,
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience,
        ttl=timedelta(minutes=settings.access_token_ttl_minutes),
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    """App factory: lets tests build isolated apps with custom settings."""
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level)

    docs = settings.docs_on
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        debug=settings.debug,
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    # Engine creation is lazy (no connection yet), so the app starts even if the DB is down.
    app.state.engine = create_db_engine(settings)
    app.state.session_factory = create_session_factory(app.state.engine)
    app.state.storage = FileStorage(settings.storage_dir)
    # One embedder per process, shared by all requests. The model itself loads lazily on
    # the first search, so startup and the other endpoints never wait for it.
    app.state.embedder = create_embedder(settings, EMBEDDING_DIMENSION)
    # None when generation is disabled: /ask then answers 503 generation_unavailable.
    # Creating a provider opens no connection; an unreachable server is reported per request.
    app.state.generator = create_generator(settings)
    app.state.token_counter = create_token_counter(settings)
    app.state.token_settings = _token_settings(settings)

    # Added innermost first: the last one added sees the request first.
    app.add_middleware(BodySizeLimitMiddleware, max_upload_bytes=settings.max_upload_bytes)
    if origins := settings.split(settings.cors_allowed_origins):
        # Bearer tokens in headers, no cookies: credentials stay off.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
            expose_headers=["X-Request-ID", "Retry-After"],
        )
    if hosts := settings.split(settings.allowed_hosts):
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)
    app.add_middleware(
        SecurityHeadersMiddleware, api_prefix=settings.api_v1_prefix, hsts=settings.hsts_enabled
    )
    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.api_v1_prefix)
    return app


app = create_app()
