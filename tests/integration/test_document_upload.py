"""POST /api/v1/documents against the real test database and a temporary storage dir.

Unlike the rollback-per-test fixtures, the app commits for real here, so every test
starts and ends with empty tables.
"""

import hashlib
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.ingestion.file_types import DOCX_MEDIA_TYPE
from app.ingestion.storage import FileStorage
from app.main import create_app
from app.models import Document, DocumentStatus, User
from tests.samples import make_docx, make_pdf

pytestmark = pytest.mark.integration

URL = "/api/v1/documents"
MAX_UPLOAD = 256 * 1024


@pytest.fixture
def storage_dir(tmp_path: Path) -> Path:
    return tmp_path / "storage"


def _truncate(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE users CASCADE"))


@pytest.fixture
def client(test_database_url: str, pg_engine: Engine, storage_dir: Path) -> Iterator[TestClient]:
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url=test_database_url,
        storage_dir=storage_dir,
        max_upload_bytes=MAX_UPLOAD,
    )
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    _truncate(pg_engine)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    _truncate(pg_engine)


def _upload(client: TestClient, name: str, data: bytes, content_type: str | None = None):
    return client.post(
        URL, files={"file": (name, data, content_type or "application/octet-stream")}
    )


def _documents(engine: Engine) -> list[Document]:
    with Session(engine) as session:
        return list(session.scalars(select(Document)))


def _stored_files(storage_dir: Path) -> list[Path]:
    return sorted(p for p in storage_dir.rglob("*") if p.is_file())


@pytest.mark.parametrize(
    ("name", "data", "content_type", "media_type", "page_count", "pages_with_text"),
    [
        (
            "report.pdf",
            make_pdf(["Page one text", "", "Page three text"]),
            "application/pdf",
            "application/pdf",
            3,
            2,
        ),
        (
            "заметки.docx",
            make_docx(["Қазақ тілі", "Русский абзац"]),
            DOCX_MEDIA_TYPE,
            DOCX_MEDIA_TYPE,
            None,
            1,
        ),
        ("notes.txt", "Сәлем, әлем!\n\nHello".encode(), None, "text/plain", None, 1),
    ],
    ids=["pdf", "docx", "txt"],
)
def test_upload_success_persists_row_and_file(
    client: TestClient,
    pg_engine: Engine,
    storage_dir: Path,
    name: str,
    data: bytes,
    content_type: str | None,
    media_type: str,
    page_count: int | None,
    pages_with_text: int,
) -> None:
    response = _upload(client, name, data, content_type)

    assert response.status_code == 201, response.text
    body = response.json()
    doc = body["document"]
    assert doc["filename"] == name
    assert doc["content_type"] == media_type
    assert doc["file_size"] == len(data)
    assert doc["content_hash"] == hashlib.sha256(data).hexdigest()
    assert doc["status"] == "pending"
    assert doc["error_message"] is None
    assert body["extraction"]["page_count"] == page_count
    assert body["extraction"]["pages_with_text"] == pages_with_text
    assert body["extraction"]["character_count"] > 0

    [row] = _documents(pg_engine)
    assert str(row.id) == doc["id"]
    assert row.status is DocumentStatus.PENDING
    with Session(pg_engine) as session:
        assert session.get(User, row.user_id).email == "dev@docmind.local"

    stored = FileStorage(storage_dir).path_for(row.id)
    assert _stored_files(storage_dir) == [stored]
    assert stored.read_bytes() == data


def test_duplicate_upload_returns_409_with_existing_id(
    client: TestClient, pg_engine: Engine, storage_dir: Path
) -> None:
    data = b"same bytes, different names"
    first = _upload(client, "a.txt", data)
    assert first.status_code == 201

    second = _upload(client, "renamed.txt", data)

    assert second.status_code == 409
    assert second.json()["error"] == {
        "code": "duplicate_document",
        "message": "This file has already been uploaded.",
        "details": {"document_id": first.json()["document"]["id"]},
    }
    assert len(_documents(pg_engine)) == 1
    assert len(_stored_files(storage_dir)) == 1


@pytest.mark.parametrize(
    ("name", "data", "content_type", "status", "code"),
    [
        ("malware.exe", b"MZ\x90\x00", None, 415, "unsupported_file_type"),
        ("a.pdf", make_pdf(["x"]), "text/html", 415, "content_type_mismatch"),
        ("a.pdf", make_docx(["x"]), None, 422, "invalid_file_content"),
        ("a.txt", b"", None, 400, "empty_file"),
        ("a.pdf", b"%PDF-1.7\nnot really a pdf", None, 422, "corrupt_file"),
        ("a.docx", make_docx(["x"])[:200], None, 422, "corrupt_file"),
        ("a.pdf", make_pdf(["secret"], user_password="pw"), None, 422, "encrypted_document"),
        ("scan.pdf", make_pdf(["", ""]), None, 422, "no_text_extracted"),
        ("a.txt", "Привет".encode("cp1251"), None, 422, "unsupported_text_encoding"),
        ("a.txt", b"x" * (MAX_UPLOAD + 1), None, 413, "file_too_large"),
    ],
    ids=[
        "extension",
        "declared-type",
        "magic-bytes",
        "empty",
        "corrupt-pdf",
        "corrupt-docx",
        "encrypted-pdf",
        "no-text",
        "encoding",
        "too-large",
    ],
)
def test_rejected_upload_leaves_no_row_and_no_file(
    client: TestClient,
    pg_engine: Engine,
    storage_dir: Path,
    name: str,
    data: bytes,
    content_type: str | None,
    status: int,
    code: str,
) -> None:
    response = _upload(client, name, data, content_type)

    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert error["code"] == code
    # Safe errors: no server paths, no exception types leak to the client.
    assert str(storage_dir) not in response.text
    assert "Traceback" not in response.text
    assert _documents(pg_engine) == []
    assert _stored_files(storage_dir) == []


def test_path_traversal_filename_is_display_only(
    client: TestClient, pg_engine: Engine, storage_dir: Path, tmp_path: Path
) -> None:
    response = _upload(client, "../../../evil.txt", b"harmless text")

    assert response.status_code == 201
    assert response.json()["document"]["filename"] == "evil.txt"
    [row] = _documents(pg_engine)
    assert _stored_files(storage_dir) == [FileStorage(storage_dir).path_for(row.id)]
    assert not any(tmp_path.parent.rglob("evil.txt"))


def test_missing_file_field_is_validation_error(client: TestClient) -> None:
    response = client.post(URL, data={"not_file": "x"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_storage_failure_rolls_back_row(
    client: TestClient, pg_engine: Engine, storage_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_store(self: FileStorage, temp_path: Path, document_id: object) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(FileStorage, "store", broken_store)

    response = _upload(client, "a.txt", b"some text")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "disk full" not in response.text
    assert _documents(pg_engine) == []
    assert _stored_files(storage_dir) == []


def test_commit_failure_removes_stored_file(
    client: TestClient, pg_engine: Engine, storage_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Create the dev user first, so the only commit left in the request is the upload's.
    assert _upload(client, "warmup.txt", b"warm up").status_code == 201
    _truncate_documents_only(pg_engine)
    for path in _stored_files(storage_dir):
        path.unlink()

    def failing_commit(self: Session) -> None:
        raise OperationalError("COMMIT", {}, Exception("connection lost"))

    monkeypatch.setattr(Session, "commit", failing_commit)

    response = _upload(client, "a.txt", b"other text")

    assert response.status_code == 500
    assert _documents(pg_engine) == []
    assert _stored_files(storage_dir) == []


def _truncate_documents_only(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE documents CASCADE"))


def test_same_file_for_different_users_is_allowed(
    client: TestClient, pg_engine: Engine, storage_dir: Path
) -> None:
    data = b"shared bytes"
    assert _upload(client, "a.txt", data).status_code == 201
    # Re-own the first document so the dev user no longer has this hash.
    with pg_engine.begin() as conn:
        other_id = conn.execute(
            text(
                "INSERT INTO users (id, email) "
                "VALUES (gen_random_uuid(), 'other@x.io') RETURNING id"
            )
        ).scalar_one()
        conn.execute(text("UPDATE documents SET user_id = :uid"), {"uid": other_id})

    assert _upload(client, "a.txt", data).status_code == 201
    assert len(_documents(pg_engine)) == 2
    assert len(_stored_files(storage_dir)) == 2
