"""Registration, login and token-to-user resolution.

Identity comes only from a validated access token: no endpoint accepts a user id from the
client. Logs carry user ids and outcomes, never emails, passwords, hashes or tokens.
"""

import logging
import uuid

from fastapi import status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.core.security import (
    InvalidTokenError,
    TokenSettings,
    burn_verification,
    decode_access_token,
    hash_password,
    needs_rehash,
    verify_password,
)
from app.models import User

logger = logging.getLogger(__name__)

_EMAIL_CONSTRAINT = "uq_users_email"
BEARER = "Bearer"


def _unauthorized(code: str, message: str, error: str | None = None) -> AppError:
    # RFC 6750: a challenge on every 401; error="invalid_token" when a token was presented.
    challenge = f'{BEARER} error="{error}"' if error else BEARER
    return AppError(
        code, message, status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate": challenge}
    )


def missing_credentials() -> AppError:
    return _unauthorized("authentication_required", "Authentication required.")


def invalid_token() -> AppError:
    # One message for expired, tampered, wrong-audience, unknown-user ... tokens.
    return _unauthorized(
        "invalid_token", "The access token is invalid or expired.", "invalid_token"
    )


def register(session: Session, email: str, password: str) -> User:
    """email is already normalized by the request schema. Duplicate emails (including the
    legacy, password-less accounts) are refused: nobody can claim an account by signing up."""
    user = User(email=email, password_hash=hash_password(password))
    session.add(user)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        if constraint != _EMAIL_CONSTRAINT:
            raise
        # The unique constraint, not a prior SELECT, decides: concurrent sign-ups with the
        # same email get exactly one 201 and the rest 409.
        raise AppError(
            "email_already_registered",
            "An account with this email already exists.",
            status.HTTP_409_CONFLICT,
        ) from None
    logger.info("Registered user=%s", user.id)
    return user


def authenticate(session: Session, email: str, password: str) -> User:
    """Generic failure for unknown email, wrong password, password-less legacy account and
    inactive account, with the same Argon2 work in every case."""
    user = session.scalars(select(User).where(User.email == email)).one_or_none()
    if user is None or user.password_hash is None or not user.is_active:
        burn_verification(password)
        logger.info("Login failed")
        raise _unauthorized("invalid_credentials", "Invalid email or password.")
    if not verify_password(user.password_hash, password):
        logger.info("Login failed")
        raise _unauthorized("invalid_credentials", "Invalid email or password.")
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
        session.commit()
    logger.info("Login user=%s", user.id)
    return user


def user_from_token(session: Session, token: str, settings: TokenSettings) -> User:
    try:
        user_id: uuid.UUID = decode_access_token(token, settings)
    except InvalidTokenError as exc:
        # Reason class only (e.g. ExpiredSignatureError), never the token.
        logger.info("Rejected access token: %s", exc)
        raise invalid_token() from None
    user = session.get(User, user_id)
    if user is None or not user.is_active or user.password_hash is None:
        # Deleted or deactivated after the token was issued.
        logger.info("Rejected access token: no active user")
        raise invalid_token()
    return user
