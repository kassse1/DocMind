"""Request-scoped dependencies shared by API routers."""

from typing import Annotated

from fastapi import Depends, Request, status
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import get_db_session
from app.core.errors import AppError
from app.embeddings.base import Embedder
from app.ingestion.storage import FileStorage
from app.models import User
from app.rag.generation import Generator
from app.rag.tokens import TokenCounter
from app.services.users import get_or_create_user


def get_storage(request: Request) -> FileStorage:
    return request.app.state.storage


def get_embedder(request: Request) -> Embedder:
    return request.app.state.embedder


def get_generator(request: Request) -> Generator:
    generator = request.app.state.generator
    if generator is None:
        raise AppError(
            "generation_unavailable",
            "Answer generation is not configured on this server.",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return generator


def get_token_counter(request: Request) -> TokenCounter:
    return request.app.state.token_counter


def get_current_user(
    session: Annotated[Session, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> User:
    """DEVELOPMENT ONLY: every request acts as the single configured dev user.

    Phase 8 replaces this function with real authentication; endpoints already depend on
    it, so ownership checks will not need to change. In prod it refuses every request
    rather than letting all callers share one account.
    """
    if settings.environment == "prod":
        raise AppError(
            "authentication_required",
            "Authentication is not configured",
            status.HTTP_401_UNAUTHORIZED,
        )
    return get_or_create_user(session, settings.dev_user_email)
