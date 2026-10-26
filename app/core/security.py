"""Password hashing (Argon2id) and access tokens (JWT, HS256). No custom cryptography.

Passwords: argon2-cffi with the RFC 9106 "low memory" profile (Argon2id, t=3, m=64 MiB,
p=4). Hashes carry their own parameters, so a profile change only needs a rehash at the
next successful login (needs_rehash). Passwords are Unicode-NFC-normalized before hashing
and verification, so the same passphrase typed on different systems matches.

Tokens: PyJWT with an explicit algorithm allowlist (HS256 only), so "alg": "none" and
algorithm confusion are rejected by the library. Required claims: sub (user id), iat, exp,
iss, aud and typ ("access"). Times are UTC.

Never log a password, a hash, a token or an Authorization header: none of these functions
does, and their exceptions carry no secret material.
"""

import hashlib
import secrets
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.profiles import RFC_9106_LOW_MEMORY

JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_TYPE = "access"
# 256-bit minimum for an HS256 key (RFC 7518 section 3.2).
MIN_SECRET_BYTES = 32
# sid = the login session (app/models/session.py); checked on every request.
_REQUIRED_CLAIMS = ("sub", "sid", "iat", "exp", "iss", "aud", "typ")

_hasher = PasswordHasher.from_parameters(RFC_9106_LOW_MEMORY)


def _normalize(password: str) -> str:
    return unicodedata.normalize("NFC", password)


def hash_password(password: str) -> str:
    return _hasher.hash(_normalize(password))


def verify_password(password_hash: str, password: str) -> bool:
    """Constant-time comparison inside argon2; False for a mismatch or a corrupt hash."""
    try:
        return _hasher.verify(password_hash, _normalize(password))
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


# Verified against when the account does not exist or cannot log in, so a failed login
# costs the same Argon2 work either way (no timing signal for user enumeration).
_DUMMY_HASH = hash_password("docmind-dummy-password-for-timing")


def burn_verification(password: str) -> None:
    verify_password(_DUMMY_HASH, password)


@dataclass(frozen=True)
class TokenSettings:
    secret: str
    issuer: str
    audience: str
    ttl: timedelta


class InvalidTokenError(Exception):
    """Any problem with a presented token. The message never contains the token."""


@dataclass(frozen=True)
class AccessClaims:
    user_id: uuid.UUID
    session_id: uuid.UUID


def create_access_token(
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    settings: TokenSettings,
    now: datetime | None = None,
) -> str:
    issued = (now or datetime.now(UTC)).replace(microsecond=0)
    claims = {
        "sub": str(user_id),
        "sid": str(session_id),
        "iat": issued,
        "exp": issued + settings.ttl,
        "iss": settings.issuer,
        "aud": settings.audience,
        "typ": ACCESS_TOKEN_TYPE,
        # Unique id per token: useful for future revocation lists, harmless now.
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(claims, settings.secret, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str, settings: TokenSettings) -> AccessClaims:
    """Validate signature, algorithm, exp/iat, issuer, audience and type; return the ids."""
    try:
        claims = jwt.decode(
            token,
            settings.secret,
            algorithms=[JWT_ALGORITHM],
            audience=settings.audience,
            issuer=settings.issuer,
            options={"require": list(_REQUIRED_CLAIMS)},
            # Small allowance for clock skew between processes.
            leeway=timedelta(seconds=10),
        )
    except jwt.PyJWTError as exc:
        raise InvalidTokenError(type(exc).__name__) from None
    if claims.get("typ") != ACCESS_TOKEN_TYPE:
        raise InvalidTokenError("wrong token type")
    try:
        return AccessClaims(uuid.UUID(str(claims["sub"])), uuid.UUID(str(claims["sid"])))
    except ValueError:
        raise InvalidTokenError("invalid subject or session") from None


def new_refresh_token() -> str:
    """Opaque, 256 bits from the OS CSPRNG. Never a JWT: it means nothing without the
    server-side row, and only its hash is stored."""
    return secrets.token_urlsafe(32)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
