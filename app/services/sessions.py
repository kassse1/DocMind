"""Login sessions and refresh-token rotation.

Lifecycle:
- login creates an AuthSession (absolute lifetime DOCMIND_SESSION_MAX_AGE_DAYS) and its first
  refresh token (idle lifetime DOCMIND_REFRESH_TOKEN_TTL_DAYS, never past the session end);
- POST /auth/refresh exchanges a refresh token for a new access token AND a new refresh
  token; the old one is marked used;
- presenting a used refresh token again is reuse: someone holds a copy. The whole session
  (every refresh token of the family and every access token carrying its id) is revoked;
- logout revokes the current session (or all of the user's); deactivation and password
  changes revoke all of them.

Concurrency: the refresh token row and its session are locked (SELECT ... FOR UPDATE)
before the token is checked, so two requests with the same token are serialized. The
first rotates it; the second then finds it used and is treated as reuse, which revokes the
session. Strict on purpose: a client that sends the same refresh token twice in parallel
is logged out rather than letting a stolen copy race the owner.

Nothing here logs or returns a refresh token except to its owner in the response body;
the database stores only SHA-256 hashes.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import status
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError
from app.core.security import hash_refresh_token, new_refresh_token
from app.models import AuthSession, RefreshToken, User

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IssuedRefresh:
    session: AuthSession
    token: str
    expires_at: datetime


def _now() -> datetime:
    return datetime.now(UTC)


def invalid_refresh_token() -> AppError:
    # One answer for unknown, expired, revoked and reused tokens.
    return AppError(
        "invalid_refresh_token",
        "The refresh token is invalid or expired.",
        status.HTTP_401_UNAUTHORIZED,
    )


def _issue(db: Session, auth_session: AuthSession, settings: Settings) -> IssuedRefresh:
    now = _now()
    expires_at = min(now + timedelta(days=settings.refresh_token_ttl_days), auth_session.expires_at)
    token = new_refresh_token()
    db.add(
        RefreshToken(
            session_id=auth_session.id, token_hash=hash_refresh_token(token), expires_at=expires_at
        )
    )
    return IssuedRefresh(auth_session, token, expires_at)


def start_session(db: Session, user: User, settings: Settings) -> IssuedRefresh:
    auth_session = AuthSession(
        user_id=user.id, expires_at=_now() + timedelta(days=settings.session_max_age_days)
    )
    db.add(auth_session)
    db.flush()
    issued = _issue(db, auth_session, settings)
    db.commit()
    logger.info("Session started session=%s user=%s", auth_session.id, user.id)
    return issued


def rotate(db: Session, presented: str, settings: Settings) -> tuple[User, IssuedRefresh]:
    token = db.scalars(
        select(RefreshToken)
        .where(RefreshToken.token_hash == hash_refresh_token(presented))
        .with_for_update()
    ).one_or_none()
    if token is None:
        logger.info("Refresh rejected: unknown token")
        raise invalid_refresh_token()
    auth_session = db.get(AuthSession, token.session_id, with_for_update=True)
    now = _now()
    if token.used_at is not None:
        if auth_session is not None and auth_session.revoked_at is None:
            auth_session.revoked_at, auth_session.revoked_reason = now, "reuse_detected"
            db.commit()
        logger.warning("Refresh token reuse detected: session=%s revoked", token.session_id)
        raise invalid_refresh_token()
    user = db.get(User, auth_session.user_id) if auth_session else None
    if (
        auth_session is None
        or auth_session.revoked_at is not None
        or auth_session.expires_at <= now
        or token.expires_at <= now
        or user is None
        or not user.is_active
        or user.password_hash is None
    ):
        db.rollback()
        logger.info("Refresh rejected: expired or revoked session=%s", token.session_id)
        raise invalid_refresh_token()
    token.used_at = now
    auth_session.last_used_at = now
    issued = _issue(db, auth_session, settings)
    db.commit()
    logger.info("Session refreshed session=%s", auth_session.id)
    return user, issued


def active_session(db: Session, session_id: uuid.UUID, user_id: uuid.UUID) -> AuthSession | None:
    auth_session = db.get(AuthSession, session_id)
    if (
        auth_session is None
        or auth_session.user_id != user_id
        or auth_session.revoked_at is not None
        or auth_session.expires_at <= _now()
    ):
        return None
    return auth_session


def revoke(db: Session, auth_session: AuthSession, reason: str) -> None:
    if auth_session.revoked_at is None:
        auth_session.revoked_at, auth_session.revoked_reason = _now(), reason
    db.commit()
    logger.info("Session revoked session=%s reason=%s", auth_session.id, reason)


def revoke_all(db: Session, user_id: uuid.UUID, reason: str) -> int:
    """Revoke every active session of the user; the caller commits."""
    result = db.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=_now(), revoked_reason=reason)
    )
    logger.info("Sessions revoked user=%s count=%d reason=%s", user_id, result.rowcount, reason)
    return result.rowcount


def list_active(db: Session, user_id: uuid.UUID) -> list[AuthSession]:
    return list(
        db.scalars(
            select(AuthSession)
            .where(
                AuthSession.user_id == user_id,
                AuthSession.revoked_at.is_(None),
                AuthSession.expires_at > _now(),
            )
            .order_by(AuthSession.created_at.desc())
        )
    )
