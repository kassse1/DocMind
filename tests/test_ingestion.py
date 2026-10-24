"""Unit tests for file type validation, text extraction and normalization (no database)."""

from pathlib import Path

import pytest

from app.ingestion.errors import IngestionError
from app.ingestion.file_types import (
    DOCX_MEDIA_TYPE,
    FileKind,
    kind_for_media_type,
    resolve_file_type,
    sanitize_filename,
    verify_magic_bytes,
)
from app.ingestion.normalize import normalize_text
from app.ingestion.parsers import ExtractionLimits, extract_text
from tests.samples import make_docx, make_pdf, make_zip

LIMITS = ExtractionLimits(max_pdf_pages=50, max_docx_uncompressed_bytes=5 * 1024 * 1024)


def _write(tmp_path: Path, data: bytes, name: str = "upload.bin") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _error_code(action) -> str:
    with pytest.raises(IngestionError) as exc_info:
        action()
    return exc_info.value.code


# --- filenames -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("report.pdf", "report.pdf"),
        ("../../etc/passwd.txt", "passwd.txt"),
        ("..\\..\\Windows\\win.ini.txt", "win.ini.txt"),
        ("C:\\Users\\me\\Отчёт 2024.docx", "Отчёт 2024.docx"),
        ("evil\x00name\n.pdf", "evilname.pdf"),
        ("", "upload"),
        (None, "upload"),
        ("..", "upload"),
    ],
)
def test_sanitize_filename_strips_paths_and_control_chars(raw: str | None, expected: str) -> None:
    assert sanitize_filename(raw) == expected


def test_sanitize_filename_truncates_but_keeps_extension() -> None:
    name = sanitize_filename("a" * 400 + ".pdf")
    assert len(name) == 255
    assert name.endswith(".pdf")


# --- extension and declared type ---------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "declared", "kind"),
    [
        ("a.pdf", "application/pdf", FileKind.PDF),
        ("A.PDF", "application/octet-stream", FileKind.PDF),
        ("a.docx", DOCX_MEDIA_TYPE, FileKind.DOCX),
        ("a.txt", "text/plain; charset=utf-8", FileKind.TXT),
        ("a.txt", None, FileKind.TXT),
    ],
)
def test_resolve_file_type_accepts_consistent_types(
    filename: str, declared: str | None, kind: FileKind
) -> None:
    assert resolve_file_type(filename, declared).kind is kind


@pytest.mark.parametrize("filename", ["a.exe", "a.doc", "a.pdf.exe", "noext", "a.html"])
def test_resolve_file_type_rejects_unsupported_extensions(filename: str) -> None:
    assert _error_code(lambda: resolve_file_type(filename, None)) == "unsupported_file_type"


def test_resolve_file_type_rejects_conflicting_declared_type() -> None:
    code = _error_code(lambda: resolve_file_type("a.pdf", "text/html"))
    assert code == "content_type_mismatch"


# --- magic bytes ---------------------------------------------------------------------------


def test_magic_bytes_reject_docx_renamed_to_pdf() -> None:
    spec = resolve_file_type("a.pdf", None)
    data = make_docx(["hi"])
    assert _error_code(lambda: verify_magic_bytes(spec, data[:1024])) == "invalid_file_content"


def test_magic_bytes_reject_pdf_renamed_to_txt() -> None:
    spec = resolve_file_type("a.txt", None)
    data = make_pdf(["hi"])
    assert _error_code(lambda: verify_magic_bytes(spec, data[:1024])) == "invalid_file_content"


def test_magic_bytes_reject_legacy_doc_renamed_to_docx() -> None:
    spec = resolve_file_type("a.docx", None)
    ole_header = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100
    assert _error_code(lambda: verify_magic_bytes(spec, ole_header)) == "invalid_file_content"


def test_magic_bytes_accept_real_files() -> None:
    verify_magic_bytes(resolve_file_type("a.pdf", None), make_pdf(["x"])[:1024])
    verify_magic_bytes(resolve_file_type("a.docx", None), make_docx(["x"])[:1024])
    verify_magic_bytes(resolve_file_type("a.txt", None), "текст".encode())


# --- normalization -------------------------------------------------------------------------


def test_normalize_keeps_paragraphs_and_cleans_whitespace() -> None:
    raw = "  Title\r\n\r\n\r\n\r\nFirst\tline  with\u00a0spaces  \nsecond line\x0c\n\n\nNext"
    assert normalize_text(raw) == "Title\n\nFirst line with spaces\nsecond line\n\nNext"


def test_normalize_removes_invisible_and_control_chars() -> None:
    assert normalize_text("in\u00advis\u200bible\ufeff a\x00b\x07c") == "invisible a b c"


def test_normalize_applies_nfc() -> None:
    decomposed = "Й"  # И + combining breve
    assert normalize_text(decomposed) == "Й"


# --- extraction ----------------------------------------------------------------------------


def test_extract_pdf_keeps_page_numbers_and_skips_empty_pages(tmp_path: Path) -> None:
    path = _write(tmp_path, make_pdf(["First page text", "", "Third page text"]))

    result = extract_text(FileKind.PDF, path, LIMITS)

    assert result.page_count == 3
    assert [(p.page_number, p.text) for p in result.pages] == [
        (1, "First page text"),
        (3, "Third page text"),
    ]
    assert result.text == "First page text\n\nThird page text"


def test_extract_pdf_without_text_layer_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, make_pdf(["", ""]))
    assert _error_code(lambda: extract_text(FileKind.PDF, path, LIMITS)) == "no_text_extracted"


def test_extract_encrypted_pdf_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, make_pdf(["secret"], user_password="pw"))
    assert _error_code(lambda: extract_text(FileKind.PDF, path, LIMITS)) == "encrypted_document"


@pytest.mark.parametrize(
    "data",
    [b"%PDF-1.7\nthis is not really a pdf", make_pdf(["truncated text"])[:300]],
    ids=["garbage", "truncated"],
)
def test_extract_corrupt_pdf_is_rejected(tmp_path: Path, data: bytes) -> None:
    path = _write(tmp_path, data)
    assert _error_code(lambda: extract_text(FileKind.PDF, path, LIMITS)) == "corrupt_file"


def test_extract_pdf_page_limit(tmp_path: Path) -> None:
    path = _write(tmp_path, make_pdf(["x"] * 3))
    limits = ExtractionLimits(
        max_pdf_pages=2, max_docx_uncompressed_bytes=LIMITS.max_docx_uncompressed_bytes
    )
    assert _error_code(lambda: extract_text(FileKind.PDF, path, limits)) == "too_many_pages"


def test_extract_docx_paragraphs_tables_and_unicode(tmp_path: Path) -> None:
    data = make_docx(
        ["Қазақстан Республикасы", "", "Второй абзац"],
        table=[["Name", "Value"], ["alpha", "1"]],
    )
    path = _write(tmp_path, data)

    result = extract_text(FileKind.DOCX, path, LIMITS)

    assert result.page_count is None
    [page] = result.pages
    assert page.page_number is None
    assert page.text == "Қазақстан Республикасы\n\nВторой абзац\n\nName | Value\nalpha | 1"


def test_extract_invalid_docx_zip_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, make_zip({"hello.txt": b"not a word document"}))
    assert _error_code(lambda: extract_text(FileKind.DOCX, path, LIMITS)) == "corrupt_file"


def test_extract_truncated_docx_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, make_docx(["text"])[:200])
    assert _error_code(lambda: extract_text(FileKind.DOCX, path, LIMITS)) == "corrupt_file"


def test_extract_docx_zip_bomb_guard(tmp_path: Path) -> None:
    # 6 MiB of zeros compresses to a few KiB but exceeds the 5 MiB uncompressed limit.
    path = _write(tmp_path, make_zip({"word/document.xml": b"\x00" * (6 * 1024 * 1024)}))
    assert path.stat().st_size < 100 * 1024
    assert _error_code(lambda: extract_text(FileKind.DOCX, path, LIMITS)) == "archive_too_large"


@pytest.mark.parametrize(
    "data",
    [
        "Сәлем, әлем!\nПривет".encode(),
        "Сәлем, әлем!\nПривет".encode("utf-8-sig"),
        "Сәлем, әлем!\nПривет".encode("utf-16"),
    ],
    ids=["utf8", "utf8-bom", "utf16-bom"],
)
def test_extract_txt_encodings(tmp_path: Path, data: bytes) -> None:
    result = extract_text(FileKind.TXT, _write(tmp_path, data), LIMITS)
    assert result.text == "Сәлем, әлем!\nПривет"


def test_extract_txt_non_utf8_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, "Привет".encode("cp1251"))
    code = _error_code(lambda: extract_text(FileKind.TXT, path, LIMITS))
    assert code == "unsupported_text_encoding"


def test_extract_txt_binary_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, b"abc\x00\x00def")
    assert _error_code(lambda: extract_text(FileKind.TXT, path, LIMITS)) == "invalid_file_content"


def test_extract_whitespace_only_txt_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, b" \n\t\r\n ")
    assert _error_code(lambda: extract_text(FileKind.TXT, path, LIMITS)) == "no_text_extracted"


@pytest.mark.parametrize(
    ("media_type", "kind"),
    [
        ("application/pdf", FileKind.PDF),
        (DOCX_MEDIA_TYPE, FileKind.DOCX),
        ("text/plain", FileKind.TXT),
    ],
)
def test_kind_for_stored_media_type(media_type: str, kind: FileKind) -> None:
    assert kind_for_media_type(media_type) is kind


def test_kind_for_unknown_media_type_is_rejected() -> None:
    with pytest.raises(IngestionError) as exc_info:
        kind_for_media_type("application/zip")
    assert exc_info.value.code == "unsupported_file_type"
