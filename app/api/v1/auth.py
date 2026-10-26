from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_auth, get_current_user, get_token_settings
from app.core.client_ip import client_ip, parse_networks
from app.core.config import Settings, get_settings
from app.core.database import get_db_session
from app.core.errors import AppError, DatabaseUnavailableRoute
from app.core.security import TokenSettings, create_access_token
from app.models import User
from app.schemas.auth import (
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    RegisterRequest,
    SessionRead,
    TokenResponse,
    UserRead,
)
from app.services import auth as auth_service
from app.services import rate_limit, sessions

router = APIRouter(prefix="/auth", tags=["auth"], route_class=DatabaseUnavailableRoute)


def _limit(
    db: Session, settings: Settings, request: Request, scope: str, spec: str
) -> None:
    if settings.rate_limit_enabled:
        ip = client_ip(request, parse_networks(settings.trusted_proxies))
        rate_limit.enforce(db, scope, ip, rate_limit.Limit.parse(spec))


def _tokens(user: User, issued: sessions.IssuedRefresh, token_settings: TokenSettings):
    return TokenResponse(
        access_token=create_access_token(user.id, issued.session.id, token_settings),
        expires_in=int(token_settings.ttl.total_seconds()),
        refresh_token=issued.token,
        refresh_expires_in=max(0, int((issued.expires_at - datetime.now(UTC)).total_seconds())),
    )


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    response_model=UserRead,
    responses={
        409: {"description": "An account with this email already exists"},
        422: {"description": "Invalid email, password outside the policy, or extra fields"},
        429: {"description": "Too many registrations from this address"},
    },
)
def register(
    body: RegisterRequest,
    request: Request,
    session: Annotated[Session, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> UserRead:
    """Create an account. Returns the public profile; log in to get tokens."""
    _limit(session, settings, request, "register_ip", settings.rate_limit_register_per_ip)
    return UserRead.model_validate(auth_service.register(session, body.email, body.password))


@router.post(
    "/login",
    response_model=TokenResponse,
    responses={
        401: {"description": "Invalid email or password (one message for every case)"},
        429: {"description": "Too many attempts"},
    },
)
def login(
    body: LoginRequest,
    request: Request,
    session: Annotated[Session, Depends(get_db_session)],
    token_settings: Annotated[TokenSettings, Depends(get_token_settings)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> TokenResponse:
    """Start a session: a short-lived access token and a single-use refresh token."""
    _limit(session, settings, request, "login_ip", settings.rate_limit_login_per_ip)
    # Per account, only failures count: guessing one password is slowed down, while
    # someone else's attempts cannot lock out an owner who knows the password ... until
    # the failure budget is spent, which is the trade-off of any per-account limit.
    per_email = rate_limit.Limit.parse(settings.rate_limit_login_failures_per_email)
    if settings.rate_limit_enabled:
        rate_limit.ensure_below(session, "login_email", body.email, per_email)
    try:
        user = auth_service.authenticate(session, body.email, body.password)
    except AppError as exc:
        if settings.rate_limit_enabled and exc.code == "invalid_credentials":
            rate_limit.hit(session, "login_email", body.email, per_email)
        raise
    return _tokens(user, sessions.start_session(session, user, settings), token_settings)


@router.post(
    "/refresh",
    response_model=TokenResponse,
    responses={
        401: {"description": "Unknown, expired, revoked or already used refresh token"},
        429: {"description": "Too many attempts"},
    },
)
def refresh(
    body: RefreshRequest,
    request: Request,
    session: Annotated[Session, Depends(get_db_session)],
    token_settings: Annotated[TokenSettings, Depends(get_token_settings)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> TokenResponse:
    """Rotate: the presented refresh token is spent and a new pair is returned. Presenting a
    spent token again revokes the whole session (reuse detection)."""
    _limit(session, settings, request, "refresh_ip", settings.rate_limit_refresh_per_ip)
    user, issued = sessions.rotate(session, body.refresh_token, settings)
    return _tokens(user, issued, token_settings)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={401: {"description": "Missing, invalid or expired access token"}},
)
def logout(
    auth: Annotated[auth_service.Authenticated, Depends(get_current_auth)],
    session: Annotated[Session, Depends(get_db_session)],
    body: LogoutRequest | None = None,
) -> Response:
    """End this session (or all of them): its refresh tokens stop working and so do its
    access tokens, immediately."""
    if body is not None and body.all_sessions:
        sessions.revoke_all(session, auth.user.id, "logout_all")
        session.commit()
    else:
        sessions.revoke(session, auth.session, "logout")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/sessions",
    response_model=list[SessionRead],
    responses={401: {"description": "Missing, invalid or expired access token"}},
)
def list_sessions(
    auth: Annotated[auth_service.Authenticated, Depends(get_current_auth)],
    session: Annotated[Session, Depends(get_db_session)],
) -> list[SessionRead]:
    """Your active sessions (devices you are logged in on)."""
    return [
        SessionRead.model_validate(s).model_copy(update={"current": s.id == auth.session.id})
        for s in sessions.list_active(session, auth.user.id)
    ]


@router.get(
    "/me",
    response_model=UserRead,
    responses={401: {"description": "Missing, invalid or expired access token"}},
)
def me(user: Annotated[User, Depends(get_current_user)]) -> UserRead:
    return UserRead.model_validate(user)
