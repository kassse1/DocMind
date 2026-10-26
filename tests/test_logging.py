"""JSON log format, request ids and the access log."""

import json
import logging
import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.logging import JsonFormatter, request_id_var
from app.main import create_app


def _record(msg: str, **extra) -> logging.LogRecord:
    record = logging.LogRecord("app.test", logging.INFO, __file__, 1, msg, None, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_formatter_fields() -> None:
    token = request_id_var.set("abc123")
    try:
        line = JsonFormatter("worker").format(_record("hello", http_status=200, ignored="x"))
    finally:
        request_id_var.reset(token)
    data = json.loads(line)
    assert data["ts"].endswith("+00:00") and data["level"] == "INFO"
    assert (data["service"], data["logger"], data["msg"]) == ("worker", "app.test", "hello")
    assert (data["request_id"], data["http_status"]) == ("abc123", 200)
    assert "ignored" not in data  # only whitelisted structured fields


def test_exceptions_are_logged_without_their_message() -> None:
    try:
        secret = "SECRET " + "DOCUMENT TEXT"  # built at runtime, as real data would be
        raise ValueError(f"SELECT ... WHERE content = '{secret}'")
    except ValueError:
        record = _record("Unhandled error")
        import sys

        record.exc_info = sys.exc_info()
    data = json.loads(JsonFormatter("api").format(record))
    assert data["exc_type"] == "ValueError"
    assert "test_exceptions_are_logged_without_their_message" in data["stack"]
    assert "SECRET DOCUMENT TEXT" not in json.dumps(data)


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(Settings(_env_file=None, environment="test")))


def _access(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "app.access"]


def test_request_id_is_generated_and_returned(client, caplog) -> None:
    caplog.set_level(logging.INFO)
    response = client.get("/api/v1/health")
    request_id = response.headers["X-Request-ID"]
    assert len(request_id) == 32 and int(request_id, 16) >= 0
    [line] = _access(caplog)
    assert (line.http_method, line.http_route, line.http_status) == ("GET", "/api/v1/health", 200)
    assert isinstance(line.elapsed_ms, float)


def test_valid_client_request_id_is_reused(client) -> None:
    supplied = "web-" + uuid.uuid4().hex[:12]
    assert client.get("/api/v1/health", headers={"X-Request-ID": supplied}).headers[
        "X-Request-ID"
    ] == supplied


@pytest.mark.parametrize(
    "bad", ["has space", "x" * 65, "inject\"}{\"level\":\"CRITICAL", "a\tb", "a/b"]
)
def test_malicious_request_ids_are_replaced(client, caplog, bad) -> None:
    caplog.set_level(logging.INFO)
    returned = client.get("/api/v1/health", headers={"X-Request-ID": bad}).headers["X-Request-ID"]
    assert returned != bad and len(returned) == 32
    assert _access(caplog)[-1].client_request_id_rejected is True


def test_access_log_uses_the_route_template_not_the_raw_url(client, caplog) -> None:
    caplog.set_level(logging.INFO)
    document_id = uuid.uuid4()
    client.get(
        f"/api/v1/documents/{document_id}?email=alice@example.com&token=SECRET",
        headers={"Authorization": "Bearer SECRET-TOKEN"},
    )
    [line] = _access(caplog)
    assert line.http_route == "/api/v1/documents/{document_id}"
    assert line.http_status == 401
    everything = " ".join(
        f"{r.getMessage()} {getattr(r, 'http_route', '')}" for r in caplog.records
    )
    for leaked in (str(document_id), "alice@example.com", "SECRET", "Bearer"):
        assert leaked not in everything


def test_unmatched_paths_are_logged_without_the_path(client, caplog) -> None:
    caplog.set_level(logging.INFO)
    client.get("/api/v1/does-not-exist/alice@example.com")
    [line] = _access(caplog)
    assert (line.http_route, line.http_status) == ("unmatched", 404)
