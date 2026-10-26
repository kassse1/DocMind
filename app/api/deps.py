"""Request-scoped dependencies shared by API routers."""

from typing import Annotated

from fastapi import Depends, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.database import get_db_session
from app.core.errors import AppError
from app.core.security import TokenSettings
from app.embeddings.base import Embedder
from app.ingestion.storage import FileStorage
from app.models import User
from app.rag.generation import Generator
from app.rag.tokens import TokenCounter
from app.services import auth as auth_service

# auto_error=False: missing or non-Bearer credentials get the DocMind error envelope (401
# with WWW-Authenticate) instead of FastAPI's default 403. Also documents the scheme in
# OpenAPI ("Authorize" button in /docs).
_bearer = HTTPBearer(auto_error=False, bearerFormat="JWT")


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


def get_token_settings(request: Request) -> TokenSettings:
    return request.app.state.token_settings


def get_current_auth(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: Annotated[Session, Depends(get_db_session)],
    token_settings: Annotated[TokenSettings, Depends(get_token_settings)],
) -> auth_service.Authenticated:
    """User and login session from a validated Bearer access token, the session still
    active (not logged out, not revoked, not past its absolute expiry)."""
    if credentials is None or not credentials.credentials:
        raise auth_service.missing_credentials()
    return auth_service.from_access_token(session, credentials.credentials, token_settings)


def get_current_user(
    auth: Annotated[auth_service.Authenticated, Depends(get_current_auth)],
) -> User:
    """The authenticated user: the single source of identity for every protected route.
    Services receive this User and scope every query to user.id. Nothing in the path,
    query, body or other headers can name or override the user."""
    return auth.user
