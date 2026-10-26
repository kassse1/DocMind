from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_token_settings
from app.core.database import get_db_session
from app.core.errors import DatabaseUnavailableRoute
from app.core.security import TokenSettings, create_access_token
from app.models import User
from app.schemas.auth import LoginRequest, RegisterRequest, TokenResponse, UserRead
from app.services import auth as auth_service

router = APIRouter(prefix="/auth", tags=["auth"], route_class=DatabaseUnavailableRoute)


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    response_model=UserRead,
    responses={
        409: {"description": "An account with this email already exists"},
        422: {"description": "Invalid email, password outside the policy, or extra fields"},
    },
)
def register(
    body: RegisterRequest, session: Annotated[Session, Depends(get_db_session)]
) -> UserRead:
    """Create an account. Returns the public profile; log in to get a token."""
    return UserRead.model_validate(auth_service.register(session, body.email, body.password))


@router.post(
    "/login",
    response_model=TokenResponse,
    responses={401: {"description": "Invalid email or password (one message for every case)"}},
)
def login(
    body: LoginRequest,
    session: Annotated[Session, Depends(get_db_session)],
    token_settings: Annotated[TokenSettings, Depends(get_token_settings)],
) -> TokenResponse:
    """Exchange email and password for a short-lived Bearer access token."""
    user = auth_service.authenticate(session, body.email, body.password)
    return TokenResponse(
        access_token=create_access_token(user.id, token_settings),
        expires_in=int(token_settings.ttl.total_seconds()),
    )


@router.get(
    "/me",
    response_model=UserRead,
    responses={401: {"description": "Missing, invalid or expired access token"}},
)
def me(user: Annotated[User, Depends(get_current_user)]) -> UserRead:
    return UserRead.model_validate(user)
