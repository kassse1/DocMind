"""Rate limiting over HTTP against the real test database."""

import threading
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import create_session_factory
from app.main import create_app
from app.services import rate_limit
from tests.conftest import TEST_JWT_SECRET

pytestmark = pytest.mark.integration

PASSWORD = "a long and boring passphrase"


@pytest.fixture
def settings(test_database_url: str) -> Settings:
    return Settings(
        _env_file=None, environment="test", database_url=test_database_url,
        jwt_secret_key=TEST_JWT_SECRET,
        rate_limit_login_per_ip="5/60", rate_limit_login_failures_per_email="3/900",
        rate_limit_register_per_ip="2/3600", rate_limit_refresh_per_ip="4/60",
        trusted_proxies="127.0.0.1",
    )


@pytest.fixture(autouse=True)
def clean(pg_engine: Engine) -> Iterator[None]:
    with pg_engine.begin() as conn:
        conn.execute(text("TRUNCATE users CASCADE"))
    yield
    with pg_engine.begin() as conn:
        conn.execute(text("TRUNCATE users CASCADE"))


def _client(settings: Settings, peer: str = "203.0.113.10") -> TestClient:
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app, raise_server_exceptions=False, client=(peer, 50000))


def _register(client: TestClient, email: str = "alice@example.com"):
    return client.post("/api/v1/auth/register", json={"email": email, "password": PASSWORD})


def _login(client: TestClient, password: str = PASSWORD, headers=None):
    return client.post(
        "/api/v1/auth/login", json={"email": "alice@example.com", "password": password},
        headers=headers or {},
    )


def test_register_burst_is_limited_per_ip(settings) -> None:
    with _client(settings) as client:
        codes = [_register(client, f"u{i}@example.com").status_code for i in range(4)]
        limited = _register(client, "late@example.com")
    with _client(settings, peer="198.51.100.20") as other:
        assert _register(other, "other@example.com").status_code == 201
    assert codes == [201, 201, 429, 429]
    assert limited.status_code == 429
    error = limited.json()["error"]
    assert error["code"] == "rate_limited"
    retry = int(limited.headers["Retry-After"])
    assert 1 <= retry <= 3600 and error["details"] == {"retry_after_seconds": retry}


def test_login_burst_is_limited_per_ip_even_with_correct_passwords(settings) -> None:
    with _client(settings) as client:
        _register(client)
        codes = [_login(client).status_code for _ in range(7)]
    assert codes == [200] * 5 + [429] * 2


def test_failed_logins_are_limited_per_email_across_addresses(settings) -> None:
    # Different source addresses through the trusted proxy: the per-IP limit never trips,
    # the per-account failure limit does.
    with _client(settings, peer="127.0.0.1") as client:
        _register(client)
        codes = [
            _login(client, "wrong but long enough", {"X-Forwarded-For": f"198.51.100.{i}"})
            .status_code
            for i in range(5)
        ]
        # Even the right password is refused until the window passes.
        locked = _login(client, headers={"X-Forwarded-For": "198.51.100.99"})
    assert codes == [401, 401, 401, 429, 429]
    assert locked.status_code == 429


def test_successful_logins_do_not_spend_the_email_failure_budget(settings) -> None:
    with _client(settings, peer="127.0.0.1") as client:
        _register(client)
        for i in range(4):
            assert _login(client, headers={"X-Forwarded-For": f"198.51.100.{i}"}).status_code == 200


def test_spoofed_forwarded_for_does_not_bypass_the_ip_limit(settings) -> None:
    # The peer is not a trusted proxy, so rotating X-Forwarded-For changes nothing.
    with _client(settings) as client:
        _register(client)
        codes = [
            _login(client, headers={"X-Forwarded-For": f"10.9.8.{i}"}).status_code
            for i in range(6)
        ]
    assert codes[-1] == 429


def test_refresh_is_limited_per_ip(settings) -> None:
    with _client(settings) as client:
        codes = [
            client.post("/api/v1/auth/refresh", json={"refresh_token": "x" * 20}).status_code
            for _ in range(6)
        ]
    assert codes == [401] * 4 + [429] * 2


def test_window_expiry_resets_the_counter(settings, pg_engine) -> None:
    with _client(settings) as client:
        _register(client)
        for _ in range(5):
            _login(client)
        assert _login(client).status_code == 429
        with pg_engine.begin() as conn:  # the window ends
            conn.execute(
                text("UPDATE rate_limits SET window_start = window_start - interval '1 hour'")
            )
        assert _login(client).status_code == 200


def test_disabled_rate_limiting(settings) -> None:
    off = settings.model_copy(update={"rate_limit_enabled": False})
    with _client(off) as client:
        _register(client)
        assert {_login(client).status_code for _ in range(8)} == {200}


def test_concurrent_hits_are_counted_exactly(pg_engine) -> None:
    factory = create_session_factory(pg_engine)
    limit = rate_limit.Limit(1000, 3600)
    barrier = threading.Barrier(8)
    counts: list[int] = []

    def worker() -> None:
        with factory() as db:
            barrier.wait()
            for _ in range(10):
                counts.append(rate_limit.hit(db, "test", "same-client", limit).count)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 80 increments in 8 sessions: every value 1..80 exactly once, no lost update.
    assert sorted(counts) == list(range(1, 81))


def test_limits_are_shared_by_independent_api_instances(settings) -> None:
    # Two apps = two engines and pools, like two API processes behind a load balancer.
    with _client(settings) as first, _client(settings) as second:
        _register(first)
        codes = [(first if i % 2 else second).post(
            "/api/v1/auth/login", json={"email": "alice@example.com", "password": PASSWORD}
        ).status_code for i in range(6)]
    assert codes.count(429) == 1


def test_counters_store_no_plaintext_identifiers(settings, pg_engine) -> None:
    with _client(settings) as client:
        _register(client)
        _login(client, "wrong but long enough")
    with Session(pg_engine) as session:
        keys = session.scalars(text("SELECT key FROM rate_limits")).all()
    assert keys and all(len(k) == 64 for k in keys)
    assert not any("alice" in k or "203.0.113" in k for k in keys)


def test_prune_removes_old_windows(pg_engine) -> None:
    factory = create_session_factory(pg_engine)
    with factory() as db:
        rate_limit.hit(db, "test", "old", rate_limit.Limit(5, 60))
        db.execute(text("UPDATE rate_limits SET window_start = now() - interval '2 days'"))
        rate_limit.hit(db, "test", "fresh", rate_limit.Limit(5, 60))
        assert rate_limit.prune(db) == 1
        db.commit()
        assert db.scalar(text("SELECT count(*) FROM rate_limits")) == 1
