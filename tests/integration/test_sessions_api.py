"""Refresh-token rotation, reuse detection, logout and revocation over HTTP."""

import logging
import threading
import uuid
from collections.abc import Iterator

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.admin.__main__ import PASSWORD_ENV
from app.admin.__main__ import main as admin_main
from app.core.config import Settings, get_settings
from app.core.security import hash_refresh_token
from app.main import create_app
from tests.conftest import TEST_JWT_SECRET

pytestmark = pytest.mark.integration

PASSWORD = "a long and boring passphrase"
EMAIL = "alice@example.com"


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
        assert c.post("/api/v1/auth/register", json={"email": EMAIL, "password": PASSWORD})\
            .status_code == 201
        yield c


def login(client: TestClient) -> dict:
    response = client.post("/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert response.status_code == 200
    return response.json()


def refresh(client: TestClient, token: str):
    return client.post("/api/v1/auth/refresh", json={"refresh_token": token})


def me(client: TestClient, access: str):
    return client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {access}"})


def _session_row(pg_engine: Engine, access: str):
    sid = jwt.decode(access, options={"verify_signature": False})["sid"]
    with pg_engine.connect() as conn:
        return conn.execute(
            text("SELECT revoked_at, revoked_reason FROM auth_sessions WHERE id = :id"),
            {"id": sid},
        ).one()


INVALID_REFRESH = {
    "error": {"code": "invalid_refresh_token",
              "message": "The refresh token is invalid or expired.", "details": None}
}


def test_login_issues_an_opaque_refresh_token_stored_only_as_a_hash(client, pg_engine):
    tokens = login(client)
    refresh_token = tokens["refresh_token"]
    assert "." not in refresh_token and len(refresh_token) >= 43  # not a JWT, 256 bits
    assert tokens["refresh_expires_in"] == pytest.approx(7 * 24 * 3600, abs=5)
    with pg_engine.connect() as conn:
        stored = conn.scalars(text("SELECT token_hash FROM refresh_tokens")).all()
    assert stored == [hash_refresh_token(refresh_token)]
    assert refresh_token not in stored


def test_refresh_rotates_and_the_old_token_is_spent(client, pg_engine) -> None:
    first = login(client)
    rotated = refresh(client, first["refresh_token"])

    assert rotated.status_code == 200
    second = rotated.json()
    assert second["refresh_token"] != first["refresh_token"]
    assert second["access_token"] != first["access_token"]
    assert me(client, second["access_token"]).status_code == 200
    # Same session: rotation does not start a new login.
    claims = [jwt.decode(t["access_token"], options={"verify_signature": False})
              for t in (first, second)]
    assert claims[0]["sid"] == claims[1]["sid"]
    with pg_engine.connect() as conn:
        used = conn.execute(text(
            "SELECT used_at IS NOT NULL FROM refresh_tokens WHERE token_hash = :h"
        ), {"h": hash_refresh_token(first["refresh_token"])}).scalar_one()
    assert used is True


def test_replayed_refresh_token_revokes_the_whole_session(client, pg_engine) -> None:
    first = login(client)
    second = refresh(client, first["refresh_token"]).json()

    replay = refresh(client, first["refresh_token"])

    assert replay.status_code == 401 and replay.json() == INVALID_REFRESH
    assert _session_row(pg_engine, second["access_token"]).revoked_reason == "reuse_detected"
    # The family is gone: the newest refresh token and the session's access tokens too.
    assert refresh(client, second["refresh_token"]).status_code == 401
    assert me(client, second["access_token"]).status_code == 401
    assert me(client, first["access_token"]).status_code == 401


def test_concurrent_refreshes_with_one_token_never_both_succeed(client, pg_engine) -> None:
    token = login(client)["refresh_token"]
    barrier = threading.Barrier(4)
    results: list[int] = []

    def attempt() -> None:
        barrier.wait()
        results.append(refresh(client, token).status_code)

    threads = [threading.Thread(target=attempt) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(200) <= 1
    assert results.count(200) + results.count(401) == 4
    # Any second use is reuse: the session ends, so no copy of the token family survives.
    with pg_engine.connect() as conn:
        assert conn.scalar(text("SELECT revoked_reason FROM auth_sessions")) == "reuse_detected"


def test_unknown_and_malformed_refresh_tokens(client) -> None:
    assert refresh(client, "not-a-real-token").json() == INVALID_REFRESH
    for body in ({}, {"refresh_token": ""}, {"refresh_token": "x" * 300},
                 {"refresh_token": "x", "user_id": str(uuid.uuid4())}):
        assert client.post("/api/v1/auth/refresh", json=body).status_code == 422


def test_expired_refresh_token_and_session(client, pg_engine) -> None:
    tokens = login(client)
    with pg_engine.begin() as conn:
        conn.execute(text("UPDATE refresh_tokens SET expires_at = now() - interval '1 second'"))
    assert refresh(client, tokens["refresh_token"]).status_code == 401

    tokens = login(client)
    with pg_engine.begin() as conn:  # absolute session lifetime reached
        conn.execute(text("UPDATE auth_sessions SET expires_at = now() - interval '1 second'"))
    assert refresh(client, tokens["refresh_token"]).status_code == 401
    assert me(client, tokens["access_token"]).status_code == 401


def test_logout_revokes_access_and_refresh_tokens_immediately(client, pg_engine) -> None:
    phone, laptop = login(client), login(client)
    headers = {"Authorization": f"Bearer {phone['access_token']}"}
    assert client.post("/api/v1/auth/logout", headers=headers).status_code == 204

    assert me(client, phone["access_token"]).status_code == 401  # no 15-minute window
    assert refresh(client, phone["refresh_token"]).status_code == 401
    assert _session_row(pg_engine, phone["access_token"]).revoked_reason == "logout"
    # Other sessions keep working.
    assert me(client, laptop["access_token"]).status_code == 200
    assert refresh(client, laptop["refresh_token"]).status_code == 200


def test_logout_all_sessions(client) -> None:
    a, b = login(client), login(client)
    response = client.post(
        "/api/v1/auth/logout", json={"all_sessions": True},
        headers={"Authorization": f"Bearer {a['access_token']}"},
    )
    assert response.status_code == 204
    for tokens in (a, b):
        assert me(client, tokens["access_token"]).status_code == 401
        assert refresh(client, tokens["refresh_token"]).status_code == 401


def test_logout_requires_authentication(client) -> None:
    assert client.post("/api/v1/auth/logout").status_code == 401


def test_sessions_list_marks_the_current_one(client) -> None:
    login(client)
    b = login(client)
    listed = client.get(
        "/api/v1/auth/sessions", headers={"Authorization": f"Bearer {b['access_token']}"}
    ).json()
    sid_b = jwt.decode(b["access_token"], options={"verify_signature": False})["sid"]
    assert len(listed) == 2
    assert [s["id"] for s in listed if s["current"]] == [sid_b]
    assert not {"refresh_token", "token_hash"} & set(listed[0])


@pytest.mark.parametrize("command", ["deactivate", "set-password"])
def test_operator_actions_revoke_every_session(
    client, pg_engine, test_database_url, monkeypatch, command
) -> None:
    tokens = login(client)
    monkeypatch.setenv("DOCMIND_DATABASE_URL", test_database_url)
    monkeypatch.setenv(PASSWORD_ENV, "a brand new long passphrase")
    assert admin_main([command, "--email", EMAIL]) == 0

    assert me(client, tokens["access_token"]).status_code == 401
    assert refresh(client, tokens["refresh_token"]).status_code == 401
    expected = "deactivated" if command == "deactivate" else "password_changed"
    assert _session_row(pg_engine, tokens["access_token"]).revoked_reason == expected


def test_refresh_tokens_never_logged(client, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    tokens = login(client)
    rotated = refresh(client, tokens["refresh_token"]).json()
    refresh(client, tokens["refresh_token"])  # reuse, logged as a warning
    messages = "\n".join(r.getMessage() for r in caplog.records)
    for secret in (tokens["refresh_token"], rotated["refresh_token"], tokens["access_token"],
                   hash_refresh_token(tokens["refresh_token"])):
        assert secret not in messages
    assert "reuse detected" in messages
