"""Decide what an upload is, using the filename extension, the declared MIME type AND the bytes.

The client controls both the filename and the Content-Type header, so neither is trusted
alone: the extension selects the expected type, and the file's magic bytes must agree.
Passing these checks only means "this looks like a PDF/DOCX/TXT"; it does not make the
file safe. Parsing still has its own limits (see parsers.py).
"""

import enum
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from app.ingestion.errors import IngestionError


class FileKind(enum.StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    TXT = "txt"


@dataclass(frozen=True)
class FileTypeSpec:
    kind: FileKind
    extension: str
    # Canonical type we store, regardless of what the client declared.
    media_type: str
    # Declared types consistent with this extension.
    declared_types: frozenset[str]


DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

SUPPORTED_TYPES: dict[str, FileTypeSpec] = {
    spec.extension: spec
    for spec in (
        FileTypeSpec(
            FileKind.PDF,
            ".pdf",
            "application/pdf",
            frozenset({"application/pdf", "application/x-pdf"}),
        ),
        FileTypeSpec(FileKind.DOCX, ".docx", DOCX_MEDIA_TYPE, frozenset({DOCX_MEDIA_TYPE})),
        FileTypeSpec(FileKind.TXT, ".txt", "text/plain", frozenset({"text/plain"})),
    )
}

# What curl, PowerShell and many HTTP clients send when they do not know the type.
# It carries no information, so it is accepted and the bytes decide.
GENERIC_MEDIA_TYPES = frozenset({"", "application/octet-stream"})

_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"
# OLE2 compound file: legacy .doc, and also password-protected .docx files.
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
HEADER_BYTES = 1024

MAX_FILENAME_LENGTH = 255
_UNSAFE_FILENAME_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def sanitize_filename(raw: str | None) -> str:
    """Return a display-only filename. It is stored in the database but never used as a path."""
    # Drop any directory part, whichever separator the client used ("../../x", "C:\\x").
    name = PureWindowsPath(PurePosixPath(raw or "").name).name
    name = unicodedata.normalize("NFC", name)
    name = _UNSAFE_FILENAME_CHARS.sub("", name).strip().strip(".")
    if not name:
        return "upload"
    if len(name) > MAX_FILENAME_LENGTH:
        suffix = Path(name).suffix[:16]
        name = name[: MAX_FILENAME_LENGTH - len(suffix)] + suffix
    return name


def _media_type(declared: str | None) -> str:
    return (declared or "").split(";", 1)[0].strip().lower()


def resolve_file_type(filename: str, declared_content_type: str | None) -> FileTypeSpec:
    """Check the extension and the declared Content-Type (cheap, before reading the file)."""
    spec = SUPPORTED_TYPES.get(Path(filename).suffix.lower())
    if spec is None:
        raise IngestionError(
            "unsupported_file_type",
            "Unsupported file type. Allowed extensions: .pdf, .docx, .txt.",
            {"allowed_extensions": sorted(SUPPORTED_TYPES)},
        )
    declared = _media_type(declared_content_type)
    if declared not in GENERIC_MEDIA_TYPES and declared not in spec.declared_types:
        raise IngestionError(
            "content_type_mismatch",
            f"Declared content type does not match the {spec.extension} extension.",
            {"expected": sorted(spec.declared_types)},
        )
    return spec


def kind_for_media_type(media_type: str) -> FileKind:
    """Map a stored (canonical) media type back to its parser, e.g. for re-extraction."""
    for spec in SUPPORTED_TYPES.values():
        if spec.media_type == media_type:
            return spec.kind
    raise IngestionError("unsupported_file_type", "Unsupported file type.")


def verify_magic_bytes(spec: FileTypeSpec, header: bytes) -> None:
    """Check that the first bytes of the file agree with the extension."""
    if spec.kind is FileKind.PDF:
        ok = _PDF_MAGIC in header[:HEADER_BYTES]
    elif spec.kind is FileKind.DOCX:
        if header.startswith(_OLE_MAGIC):
            raise IngestionError(
                "invalid_file_content",
                "Legacy .doc and password-protected Word files are not supported.",
            )
        ok = header.startswith(_ZIP_MAGIC)
    else:
        # Plain text has no signature; decoding in the parser is the real check.
        ok = not header.startswith((_PDF_MAGIC, _ZIP_MAGIC, _OLE_MAGIC))
    if not ok:
        raise IngestionError(
            "invalid_file_content", f"File content does not match the {spec.extension} extension."
        )
