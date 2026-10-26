"""Password hashing, access-token validation and the route protection audit (no database)."""

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.security import (
    InvalidTokenError,
    TokenSettings,
    create_access_token,
    decode_access_token,
    hash_password,
    needs_rehash,
    verify_password,
)
from app.main import create_app

SECRET = "unit-test-secret-0123456789abcdef-xyz"
TOKENS = TokenSettings(SECRET, "docmind", "docmind-api", timedelta(minutes=15))
USER = uuid.uuid4()


# --- passwords ---------------------------------------------------------------------------


def test_argon2id_hash_with_salt_and_verification() -> None:
    first, second = hash_password("correct horse battery"), hash_password("correct horse battery")
    assert first.startswith("$argon2id$v=19$m=65536,t=3,p=4$")
    assert first != second  # random salt per hash
    assert "correct horse battery" not in first
    assert verify_password(first, "correct horse battery")
    assert not verify_password(first, "correct horse batterY")
    assert not needs_rehash(first)


def test_unicode_passwords_are_nfc_normalized() -> None:
    composed, decomposed = "пароль-ё-қазақ-é", "пароль-ё-қазақ-é"
    assert composed != decomposed
    assert verify_password(hash_password(composed), decomposed)


@pytest.mark.parametrize("bad_hash", ["", "not-a-hash", "$argon2id$v=19$m=1,t=1,p=1$xx$yy"])
def test_corrupt_hash_never_verifies(bad_hash: str) -> None:
    assert verify_password(bad_hash, "anything at all") is False


# --- tokens ------------------------------------------------------------------------------


def _claims(**overrides):
    now = datetime.now(UTC)
    claims = {
        "sub": str(USER), "iat": now, "exp": now + timedelta(minutes=5),
        "iss": "docmind", "aud": "docmind-api", "typ": "access",
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


def _encode(claims, key=SECRET, algorithm="HS256") -> str:
    return jwt.encode(claims, key, algorithm=algorithm)


def test_valid_token_round_trip() -> None:
    token = create_access_token(USER, TOKENS)
    assert decode_access_token(token, TOKENS) == USER
    claims = jwt.decode(token, SECRET, algorithms=["HS256"], audience="docmind-api")
    assert claims["exp"] - claims["iat"] == 15 * 60
    assert claims["typ"] == "access" and claims["iss"] == "docmind"


def _tampered() -> str:
    head, body, signature = create_access_token(USER, TOKENS).split(".")
    return ".".join([head, body, ("A" if signature[0] != "A" else "B") + signature[1:]])


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(lambda: _encode(_claims(exp=datetime.now(UTC) - timedelta(minutes=1),
                                             iat=datetime.now(UTC) - timedelta(minutes=20))),
                     id="expired"),
        pytest.param(_tampered, id="tampered-signature"),
        pytest.param(lambda: _encode(_claims(), key="another-secret-0123456789abcdefghij"),
                     id="wrong-key"),
        pytest.param(lambda: _encode(_claims(), algorithm="HS512"), id="hs512-not-allowed"),
        pytest.param(lambda: jwt.encode(_claims(), None, algorithm="none"), id="alg-none"),
        pytest.param(lambda: _encode(_claims(iss="someone-else")), id="wrong-issuer"),
        pytest.param(lambda: _encode(_claims(aud="other-api")), id="wrong-audience"),
        pytest.param(lambda: _encode(_claims(typ="refresh")), id="wrong-type"),
        pytest.param(lambda: _encode(_claims(sub="not-a-uuid")), id="invalid-subject"),
        pytest.param(lambda: _encode(_claims(exp=None)), id="missing-exp"),
        pytest.param(lambda: _encode(_claims(sub=None)), id="missing-sub"),
        pytest.param(lambda: _encode(_claims(typ=None)), id="missing-type"),
        pytest.param(lambda: _encode(_claims(aud=None)), id="missing-audience"),
        pytest.param(lambda: "not.a.jwt", id="garbage"),
        pytest.param(lambda: "", id="empty"),
    ],
)
def test_invalid_tokens_are_rejected(token) -> None:
    with pytest.raises(InvalidTokenError) as exc_info:
        decode_access_token(token(), TOKENS)
    assert SECRET not in str(exc_info.value)


def test_secret_policy() -> None:
    with pytest.raises(ValidationError, match="at least 32 bytes"):
        Settings(_env_file=None, jwt_secret_key="short")
    with pytest.raises(ValidationError, match="required when environment is prod"):
        Settings(_env_file=None, environment="prod")
    assert Settings(_env_file=None, environment="prod", jwt_secret_key="x" * 32)
    # dev/test without a secret: a random key per app, never a fixed default.
    first = create_app(Settings(_env_file=None, environment="test")).state.token_settings
    second = create_app(Settings(_env_file=None, environment="test")).state.token_settings
    assert first.secret != second.secret and len(first.secret) >= 32


# --- every route is protected unless explicitly public ------------------------------------


PUBLIC = {
    ("GET", "/api/v1/health"),
    ("GET", "/api/v1/health/ready"),
    ("POST", "/api/v1/auth/register"),
    ("POST", "/api/v1/auth/login"),
}


def test_all_routes_except_the_public_ones_require_authentication() -> None:
    # The Bearer scheme is wired only through get_current_user, so an operation carries an
    # OpenAPI security requirement exactly when it depends on the authenticated user.
    app = create_app(Settings(_env_file=None, environment="test"))
    operations = {
        (method.upper(), path): bool(operation.get("security"))
        for path, item in app.openapi()["paths"].items()
        for method, operation in item.items()
    }
    assert operations.keys() >= PUBLIC
    assert {key for key, protected in operations.items() if not protected} == PUBLIC
    assert {
        ("POST", "/api/v1/documents"), ("GET", "/api/v1/documents"),
        ("GET", "/api/v1/documents/{document_id}"), ("DELETE", "/api/v1/documents/{document_id}"),
        ("POST", "/api/v1/search"), ("POST", "/api/v1/search/hybrid"), ("POST", "/api/v1/ask"),
        ("GET", "/api/v1/auth/me"),
    } <= {key for key, protected in operations.items() if protected}
    # Every protected operation documents its 401.
    for key, protected in operations.items():
        if protected:
            method, path = key
            assert "401" in app.openapi()["paths"][path][method.lower()]["responses"], key
    # No operation takes a user id from the client: identity has one source.
    for path, item in app.openapi()["paths"].items():
        for operation in item.values():
            names = {p["name"].lower() for p in operation.get("parameters", [])}
            assert not names & {"user_id", "x-user-id", "owner_id"}, path
