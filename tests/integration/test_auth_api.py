"""Authentication over HTTP against the real test database."""

import logging
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from app.admin.__main__ import PASSWORD_ENV
from app.admin.__main__ import main as admin_main
from app.core.config import Settings, get_settings
from app.core.database import create_session_factory
from app.core.errors import AppError
from app.core.security import create_access_token
from app.main import create_app
from app.models import User
from app.services import auth as auth_service
from tests.conftest import TEST_JWT_SECRET

pytestmark = pytest.mark.integration

PASSWORD = "a long and boring passphrase"


@pytest.fixture
def settings(test_database_url: str) -> Settings:
    return Settings(
        _env_file=None, environment="test", database_url=test_database_url,
        jwt_secret_key=TEST_JWT_SECRET,
    )


@pytest.fixture(autouse=True)
def clean(pg_engine: Engine) -> Iterator[None]:
    def truncate() -> None:
        with pg_engine.begin() as conn:
            conn.execute(text("TRUNCATE users CASCADE"))

    truncate()
    yield
    truncate()


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def register(client: TestClient, email: str = "alice@example.com", password: str = PASSWORD):
    return client.post("/api/v1/auth/register", json={"email": email, "password": password})


def login(client: TestClient, email: str = "alice@example.com", password: str = PASSWORD):
    return client.post("/api/v1/auth/login", json={"email": email, "password": password})


def me(client: TestClient, token: str | None = None, header: str | None = None):
    headers = {"Authorization": header or f"Bearer {token}"} if (token or header) else {}
    return client.get("/api/v1/auth/me", headers=headers)


# --- registration ------------------------------------------------------------------------


def test_register_returns_public_profile_and_stores_argon2id(client, pg_engine) -> None:
    response = register(client, "  Alice@Example.COM ")

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"id", "email", "created_at"}
    assert body["email"] == "alice@example.com"
    assert PASSWORD not in response.text and "argon2" not in response.text
    with Session(pg_engine) as session:
        user = session.get(User, uuid.UUID(body["id"]))
        assert user.password_hash.startswith("$argon2id$")
        assert PASSWORD not in user.password_hash
        assert user.is_active is True


@pytest.mark.parametrize(
    "variant", ["alice@example.com", "ALICE@example.com", " alice@EXAMPLE.com"]
)
def test_duplicate_emails_are_rejected_after_normalization(client, variant) -> None:
    assert register(client).status_code == 201
    response = register(client, variant, "another long passphrase")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "email_already_registered"


def test_concurrent_duplicate_registrations_create_one_account(settings, pg_engine) -> None:
    factory = create_session_factory(pg_engine)
    barrier = threading.Barrier(6)
    outcomes: list[str] = []

    def attempt() -> None:
        with factory() as session:
            barrier.wait()
            try:
                auth_service.register(session, "race@example.com", PASSWORD)
                outcomes.append("created")
            except AppError as exc:
                outcomes.append(f"{exc.status_code}:{exc.code}")

    threads = [threading.Thread(target=attempt) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(outcomes) == ["409:email_already_registered"] * 5 + ["created"]
    with Session(pg_engine) as session:
        assert session.scalar(text("SELECT count(*) FROM users")) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"email": "alice@example.com"},
        {"password": PASSWORD},
        {"email": "not-an-email", "password": PASSWORD},
        {"email": "a@b", "password": PASSWORD},
        {"email": "x" * 320 + "@example.com", "password": PASSWORD},
        {"email": "alice@example.com", "password": "short"},
        {"email": "alice@example.com", "password": " " * 20},
        {"email": "alice@example.com", "password": "x" * 1025},
        {"email": "alice@example.com", "password": PASSWORD, "is_admin": True},
        {"email": "alice@example.com", "password": PASSWORD, "user_id": str(uuid.uuid4())},
    ],
)
def test_malformed_registrations_are_422_without_echoing_secrets(client, payload) -> None:
    response = client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert PASSWORD not in response.text


# --- login -------------------------------------------------------------------------------


def test_login_returns_a_short_lived_bearer_token(client) -> None:
    user_id = register(client).json()["id"]
    response = login(client, "ALICE@example.com")

    assert response.status_code == 200
    body = response.json()
    assert (body["token_type"], body["expires_in"]) == ("bearer", 15 * 60)
    claims = jwt.decode(
        body["access_token"], TEST_JWT_SECRET, algorithms=["HS256"], audience="docmind-api"
    )
    assert (claims["sub"], claims["iss"], claims["typ"]) == (user_id, "docmind", "access")
    assert claims["exp"] - claims["iat"] == 15 * 60
    assert me(client, body["access_token"]).json()["id"] == user_id


def test_login_failures_are_indistinguishable(client, pg_engine) -> None:
    register(client)
    with pg_engine.begin() as conn:  # a legacy, password-less account
        conn.execute(text("INSERT INTO users (id, email) VALUES (:id, 'legacy@example.com')"),
                     {"id": uuid.uuid4()})
    wrong_password = login(client, password="not the right passphrase")
    unknown = login(client, "nobody@example.com")
    legacy = login(client, "legacy@example.com")

    for response in (wrong_password, unknown, legacy):
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"
    assert wrong_password.json() == unknown.json() == legacy.json() == {
        "error": {"code": "invalid_credentials", "message": "Invalid email or password.",
                  "details": None}
    }


def test_malformed_login_is_422(client) -> None:
    for payload in ({}, {"email": "x"}, {"email": "a@example.com", "password": ""},
                    {"email": "a@example.com", "password": "p", "extra": 1}):
        assert client.post("/api/v1/auth/login", json=payload).status_code == 422


def test_inactive_user_cannot_log_in_and_existing_tokens_stop_working(client, pg_engine):
    register(client)
    token = login(client).json()["access_token"]
    assert me(client, token).status_code == 200
    with pg_engine.begin() as conn:
        conn.execute(text("UPDATE users SET is_active = false"))

    assert login(client).status_code == 401
    response = me(client, token)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


def test_token_of_a_deleted_user_is_rejected(client, pg_engine) -> None:
    register(client)
    token = login(client).json()["access_token"]
    with pg_engine.begin() as conn:
        conn.execute(text("DELETE FROM users"))
    assert me(client, token).status_code == 401


# --- legacy accounts ---------------------------------------------------------------------


def test_legacy_account_cannot_be_claimed_by_registration_only_by_the_operator(
    client, pg_engine, test_database_url, monkeypatch, capsys
) -> None:
    legacy_id = uuid.uuid4()
    with pg_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES (:id, 'dev@docmind.local')"),
                     {"id": legacy_id})
    assert register(client, "dev@docmind.local").status_code == 409
    assert login(client, "dev@docmind.local").status_code == 401

    # Operator claims it on the server (test database only; password via env, not argv).
    monkeypatch.setenv("DOCMIND_DATABASE_URL", test_database_url)
    monkeypatch.setenv(PASSWORD_ENV, PASSWORD)
    assert admin_main(["set-password", "--email", "DEV@docmind.local"]) == 0
    assert PASSWORD not in capsys.readouterr().out

    response = login(client, "dev@docmind.local")
    assert response.status_code == 200
    assert me(client, response.json()["access_token"]).json()["id"] == str(legacy_id)


def test_admin_rejects_weak_password_and_unknown_account(test_database_url, monkeypatch, pg_engine):
    with pg_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES (:id, 'dev@docmind.local')"),
                     {"id": uuid.uuid4()})
    monkeypatch.setenv("DOCMIND_DATABASE_URL", test_database_url)
    monkeypatch.setenv(PASSWORD_ENV, "short")
    with pytest.raises(SystemExit, match="policy"):
        admin_main(["set-password", "--email", "dev@docmind.local"])
    with pytest.raises(SystemExit, match="No account"):
        admin_main(["deactivate", "--email", "nobody@example.com"])
    with Session(pg_engine) as session:
        assert session.scalars(select(User.password_hash)).one() is None


# --- token presentation over HTTP --------------------------------------------------------


def test_missing_and_non_bearer_credentials_are_401_with_a_challenge(client) -> None:
    for response in (me(client), me(client, header="Basic YWxpY2U6c2VjcmV0"),
                     me(client, header="Bearer"), me(client, header="Token abc")):
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"].startswith("Bearer")
        assert response.json()["error"]["code"] == "authentication_required"


def test_invalid_tokens_are_401_invalid_token(client, settings) -> None:
    user_id = uuid.UUID(register(client).json()["id"])
    good = login(client).json()["access_token"]
    sid = uuid.UUID(jwt.decode(good, options={"verify_signature": False})["sid"])
    app = client.app
    past = datetime.now(UTC) - timedelta(hours=1)
    for token in (
        create_access_token(user_id, sid, app.state.token_settings, now=past),  # expired
        good[:-2] + ("AA" if good[-2:] != "AA" else "BB"),  # tampered
        jwt.encode({"sub": str(user_id), "sid": str(sid), "typ": "access"}, None, "none"),
        create_access_token(uuid.uuid4(), sid, app.state.token_settings),  # unknown user
        create_access_token(user_id, uuid.uuid4(), app.state.token_settings),  # no session
    ):
        response = me(client, token)
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == 'Bearer error="invalid_token"'
        assert response.json()["error"]["code"] == "invalid_token"
        assert token not in response.text


def test_identity_cannot_be_overridden_by_headers_or_query(client) -> None:
    alice = register(client).json()["id"]
    bob = register(client, "bob@example.com").json()["id"]
    token = login(client).json()["access_token"]
    response = client.get(
        "/api/v1/auth/me", params={"user_id": bob},
        headers={"Authorization": f"Bearer {token}", "X-User-ID": bob},
    )
    assert response.json()["id"] == alice


# --- privacy -----------------------------------------------------------------------------


def test_no_secret_material_in_logs(client, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    register(client)
    token = login(client).json()["access_token"]
    login(client, password="wrong but long enough")
    me(client, token)
    me(client, token + "x")
    messages = "\n".join(r.getMessage() for r in caplog.records)
    for secret in (PASSWORD, "wrong but long enough", token, TEST_JWT_SECRET, "$argon2id$",
                   "alice@example.com"):
        assert secret not in messages
