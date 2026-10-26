"""Security headers, docs exposure, trusted hosts, CORS and prod configuration guards."""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.main import create_app

PROD = {"environment": "prod", "jwt_secret_key": "x" * 32, "allowed_hosts": "testserver"}


def _client(**settings) -> TestClient:
    return TestClient(create_app(Settings(_env_file=None, **settings)))


def test_api_responses_carry_security_headers() -> None:
    response = _client(environment="test").get("/api/v1/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    csp = response.headers["Content-Security-Policy"]
    assert csp == "default-src 'none'; frame-ancestors 'none'"
    assert response.headers["Cache-Control"] == "no-store"
    assert "Strict-Transport-Security" not in response.headers


def test_error_responses_carry_them_too() -> None:
    response = _client(environment="test").get("/api/v1/nope")
    assert response.status_code == 404
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_hsts_only_when_enabled() -> None:
    response = _client(environment="test", hsts_enabled=True).get("/api/v1/health")
    assert response.headers["Strict-Transport-Security"].startswith("max-age=31536000")


def test_docs_on_in_development_without_the_api_csp() -> None:
    client = _client(environment="test")
    docs = client.get("/docs")
    assert docs.status_code == 200
    assert "Content-Security-Policy" not in docs.headers  # Swagger UI needs its scripts
    assert client.get("/openapi.json").status_code == 200


def test_docs_off_in_prod_unless_enabled() -> None:
    client = _client(**PROD)
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404
    assert _client(**PROD, docs_enabled=True).get("/openapi.json").status_code == 200


def test_unknown_host_headers_are_rejected_when_hosts_are_configured() -> None:
    client = _client(**PROD)
    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/api/v1/health", headers={"Host": "evil.example"}).status_code == 400


def test_no_cors_headers_by_default() -> None:
    response = _client(environment="test").get(
        "/api/v1/health", headers={"Origin": "https://evil.example"}
    )
    assert "access-control-allow-origin" not in response.headers


def test_cors_only_for_configured_origins() -> None:
    client = _client(environment="test", cors_allowed_origins="https://app.example")
    preflight = {"Access-Control-Request-Method": "POST",
                 "Access-Control-Request-Headers": "authorization"}
    allowed = client.options("/api/v1/ask", headers={"Origin": "https://app.example", **preflight})
    denied = client.options("/api/v1/ask", headers={"Origin": "https://evil.example", **preflight})
    assert allowed.headers["access-control-allow-origin"] == "https://app.example"
    assert "access-control-allow-credentials" not in allowed.headers
    assert "access-control-allow-origin" not in denied.headers


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"debug": True}, "debug must be false"),
        ({"allowed_hosts": ""}, "allowed_hosts"),
        ({"allowed_hosts": "*"}, "allowed_hosts"),
        ({"cors_allowed_origins": "*"}, "cors_allowed_origins"),
    ],
)
def test_unsafe_prod_configurations_are_refused(overrides, message) -> None:
    with pytest.raises(ValidationError, match=message):
        Settings(_env_file=None, **{**PROD, **overrides})
