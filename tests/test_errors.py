from fastapi import FastAPI, status
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.core.config import Settings
from app.core.errors import AppError
from app.main import create_app


def _assert_envelope(body: dict, code: str) -> None:
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}
    assert body["error"]["code"] == code


def _app_with_test_routes(settings: Settings) -> FastAPI:
    app = create_app(settings)

    class Payload(BaseModel):
        text: str
        count: int

    @app.get("/_test/app-error")
    def raise_app_error() -> None:
        raise AppError("document_not_found", "Document not found", status.HTTP_404_NOT_FOUND)

    @app.get("/_test/boom")
    def raise_unhandled() -> None:
        raise RuntimeError("secret internal detail")

    @app.post("/_test/validate")
    def validate(payload: Payload) -> Payload:
        return payload

    return app


def test_unknown_route_uses_envelope(client: TestClient) -> None:
    response = client.get("/api/v1/does-not-exist")

    assert response.status_code == 404
    _assert_envelope(response.json(), "not_found")


def test_wrong_method_uses_envelope(client: TestClient) -> None:
    response = client.post("/api/v1/health")

    assert response.status_code == 405
    _assert_envelope(response.json(), "method_not_allowed")


def test_app_error_uses_envelope(settings: Settings) -> None:
    with TestClient(_app_with_test_routes(settings)) as c:
        response = c.get("/_test/app-error")

    assert response.status_code == 404
    body = response.json()
    _assert_envelope(body, "document_not_found")
    assert body["error"]["message"] == "Document not found"


def test_validation_error_does_not_echo_input(settings: Settings) -> None:
    with TestClient(_app_with_test_routes(settings)) as c:
        response = c.post("/_test/validate", json={"text": "CONFIDENTIAL", "count": "x"})

    assert response.status_code == 422
    body = response.json()
    _assert_envelope(body, "validation_error")
    assert "CONFIDENTIAL" not in response.text
    assert body["error"]["details"][0]["loc"] == ["body", "count"]


def test_unhandled_error_hides_internals(settings: Settings) -> None:
    with TestClient(_app_with_test_routes(settings), raise_server_exceptions=False) as c:
        response = c.get("/_test/boom")

    assert response.status_code == 500
    _assert_envelope(response.json(), "internal_error")
    assert "secret internal detail" not in response.text
