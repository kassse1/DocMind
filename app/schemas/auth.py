import re
import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

# Password policy: long passphrases welcome, any characters (Unicode, spaces). The upper
# bound limits the work one request can cause; Argon2 cost does not depend on length much,
# but a multi-megabyte "password" should never reach the hasher.
MIN_PASSWORD_CHARS = 12
MAX_PASSWORD_CHARS = 1024
MAX_EMAIL_CHARS = 320

# Deliberately simple: one "@", non-empty local part, a dotted domain, no whitespace. Full
# RFC 5322 parsing accepts addresses no mail system delivers to; deliverability is not
# checked (that would need DNS and an email flow).
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(value: str) -> str:
    value = value.strip().lower()
    if not _EMAIL.match(value):
        raise ValueError("not a valid email address")
    return value


Email = Annotated[
    str, StringConstraints(max_length=MAX_EMAIL_CHARS), AfterValidator(normalize_email)
]


def _not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("password must not be only whitespace")
    return value


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: Email
    password: Annotated[
        str,
        StringConstraints(min_length=MIN_PASSWORD_CHARS, max_length=MAX_PASSWORD_CHARS),
        AfterValidator(_not_blank),
    ] = Field(description=f"{MIN_PASSWORD_CHARS} to {MAX_PASSWORD_CHARS} characters, any kind")


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: Email
    # No minimum here: a policy change must not lock users out with a confusing 422.
    password: Annotated[str, StringConstraints(min_length=1, max_length=MAX_PASSWORD_CHARS)]


class UserRead(BaseModel):
    """Public profile. Never includes the password hash."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = Field(description="Seconds until the access token expires")
