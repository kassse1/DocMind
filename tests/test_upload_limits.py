"""Upload guards that act before any database or storage access (no PostgreSQL needed)."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.body_limit import MULTIPART_OVERHEAD_BYTES
from app.core.config import Settings, get_settings
from app.main import create_app

MAX_UPLOAD = 32 * 1024
UNREACHABLE_DB = "postgresql+psycopg://docmind:pw@127.0.0.1:1/docmind"
URL = "/api/v1/documents"


def _client(settings: Settings) -> TestClient:
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def storage_dir(tmp_path: Path) -> Path:
    return tmp_path / "storage"


@pytest.fixture
def limited_client(storage_dir: Path) -> Iterator[TestClient]:
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url=UNREACHABLE_DB,
        storage_dir=storage_dir,
        max_upload_bytes=MAX_UPLOAD,
    )
    with _client(settings) as c:
        yield c


def test_oversized_content_length_rejected_before_reading(
    limited_client: TestClient, storage_dir: Path
) -> None:
    data = b"a" * (MAX_UPLOAD + MULTIPART_OVERHEAD_BYTES + 1)

    response = limited_client.post(URL, files={"file": ("big.txt", data, "text/plain")})

    assert response.status_code == 413
    assert response.json()["error"] == {
        "code": "payload_too_large",
        "message": "Request body exceeds the upload size limit",
        "details": {"max_upload_bytes": MAX_UPLOAD},
    }
    assert not storage_dir.exists()


def _chunked_multipart(total_file_bytes: int) -> Iterator[bytes]:
    yield (
        b'--b\r\nContent-Disposition: form-data; name="file"; filename="big.txt"\r\n'
        b"Content-Type: text/plain\r\n\r\n"
    )
    chunk = b"a" * 8192
    for _ in range(total_file_bytes // len(chunk)):
        yield chunk
    yield b"\r\n--b--\r\n"


def test_oversized_chunked_body_rejected_while_streaming(
    limited_client: TestClient, storage_dir: Path
) -> None:
    # A generator body has no Content-Length, so only the streaming byte count can stop it.
    response = limited_client.post(
        URL,
        content=_chunked_multipart(MAX_UPLOAD * 4),
        headers={"Content-Type": "multipart/form-data; boundary=b"},
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"
    assert not storage_dir.exists()


def test_uploads_require_authentication(storage_dir: Path) -> None:
    settings = Settings(
        _env_file=None, environment="prod", database_url=UNREACHABLE_DB, storage_dir=storage_dir,
        jwt_secret_key="x" * 32,
    )
    with _client(settings) as c:
        response = c.post(URL, files={"file": ("a.txt", b"hello", "text/plain")})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"
    assert not storage_dir.exists()
