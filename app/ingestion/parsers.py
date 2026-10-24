"""Extract plain text (with page numbers where the format has pages) from PDF, DOCX and TXT.

Uploaded files are untrusted: every parser failure becomes an IngestionError with a safe
message, and resource limits cap what a small malicious file can make us do.
"""

import codecs
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import docx
import pymupdf
from docx.table import Table
from docx.text.paragraph import Paragraph

from app.ingestion.errors import IngestionError
from app.ingestion.file_types import FileKind
from app.ingestion.normalize import normalize_text

# MuPDF otherwise prints repair warnings for damaged files straight to stderr.
pymupdf.TOOLS.mupdf_display_errors(False)
pymupdf.TOOLS.mupdf_display_warnings(False)

# Join words hyphenated across line breaks and drop text outside the page. Ligatures
# ("ﬁ") are expanded because TEXT_PRESERVE_LIGATURES is deliberately not set.
_PDF_TEXT_FLAGS = pymupdf.TEXT_DEHYPHENATE | pymupdf.TEXT_MEDIABOX_CLIP
_MAX_ZIP_ENTRIES = 10_000


@dataclass(frozen=True)
class ExtractionLimits:
    max_pdf_pages: int
    # Total uncompressed size of all DOCX archive members (zip bomb guard).
    max_docx_uncompressed_bytes: int


@dataclass(frozen=True)
class ExtractedPage:
    # 1-based for PDF; None for formats without pages (DOCX, TXT).
    page_number: int | None
    text: str


@dataclass(frozen=True)
class ExtractedText:
    # Only pages that contain text; page numbers keep their original values.
    pages: list[ExtractedPage]
    # Pages in the source file (PDF only), including pages without text.
    page_count: int | None

    @property
    def text(self) -> str:
        return "\n\n".join(page.text for page in self.pages)

    @property
    def character_count(self) -> int:
        return len(self.text)


def _corrupt(kind: str) -> IngestionError:
    return IngestionError("corrupt_file", f"The {kind} file is damaged or could not be read.")


def _extract_pdf(path: Path, limits: ExtractionLimits) -> tuple[list[ExtractedPage], int]:
    try:
        # Open from memory (size is already bounded by the upload limit): when opening a
        # damaged file by path, PyMuPDF can keep the OS handle open, which on Windows
        # blocks deleting the temp file afterwards.
        document = pymupdf.open(stream=path.read_bytes(), filetype="pdf")
    except Exception as exc:  # PyMuPDF raises several unrelated exception types
        raise _corrupt("PDF") from exc

    with document:
        if document.needs_pass:
            raise IngestionError(
                "encrypted_document", "Password-protected PDF files are not supported."
            )
        if document.page_count > limits.max_pdf_pages:
            raise IngestionError(
                "too_many_pages",
                f"The PDF has more than {limits.max_pdf_pages} pages.",
                {"max_pages": limits.max_pdf_pages},
            )
        try:
            pages = [
                ExtractedPage(page.number + 1, page.get_text("text", flags=_PDF_TEXT_FLAGS))
                for page in document
            ]
        except Exception as exc:
            raise _corrupt("PDF") from exc
        return pages, document.page_count


def _check_docx_archive(path: Path, limits: ExtractionLimits) -> None:
    # Declared sizes are a safe upper bound: zipfile stops at the declared size and
    # fails the CRC check if a member tries to decompress to more.
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
    except (zipfile.BadZipFile, OSError) as exc:
        raise _corrupt("DOCX") from exc
    total = sum(member.file_size for member in members)
    if len(members) > _MAX_ZIP_ENTRIES or total > limits.max_docx_uncompressed_bytes:
        raise IngestionError(
            "archive_too_large", "The DOCX file expands to more data than is allowed."
        )


def _table_text(table: Table) -> str:
    rows = []
    for row in table.rows:
        cells, seen = [], set()
        for cell in row.cells:
            # Merged cells are returned once per spanned grid column; keep one copy.
            if id(cell._tc) not in seen:
                seen.add(id(cell._tc))
                cells.append(cell.text)
        rows.append(" | ".join(cells))
    return "\n".join(rows)


def _extract_docx(path: Path, limits: ExtractionLimits) -> tuple[list[ExtractedPage], None]:
    _check_docx_archive(path, limits)
    try:
        # python-docx parses XML with entity resolution disabled (no XXE / billion laughs).
        document = docx.Document(str(path))
        blocks = [
            item.text if isinstance(item, Paragraph) else _table_text(item)
            for item in document.iter_inner_content()
            if isinstance(item, Paragraph | Table)
        ]
    except Exception as exc:
        raise _corrupt("DOCX") from exc
    # Each Word paragraph (or table) is its own block, separated by a blank line.
    return [ExtractedPage(None, "\n\n".join(blocks))], None


def _extract_txt(path: Path, _limits: ExtractionLimits) -> tuple[list[ExtractedPage], None]:
    data = path.read_bytes()  # bounded: the upload size limit was enforced before this
    if data.startswith(codecs.BOM_UTF8):
        encoding = "utf-8-sig"
    elif data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encoding = "utf-16"
    else:
        encoding = "utf-8"
    try:
        text = data.decode(encoding)
    except UnicodeDecodeError as exc:
        raise IngestionError(
            "unsupported_text_encoding", "Text files must be UTF-8 (or UTF-16 with a BOM)."
        ) from exc
    if "\x00" in text:
        raise IngestionError("invalid_file_content", "The file looks like binary data, not text.")
    return [ExtractedPage(None, text)], None


_EXTRACTORS: dict[
    FileKind, Callable[[Path, ExtractionLimits], tuple[list[ExtractedPage], int | None]]
] = {
    FileKind.PDF: _extract_pdf,
    FileKind.DOCX: _extract_docx,
    FileKind.TXT: _extract_txt,
}


def extract_text(kind: FileKind, path: Path, limits: ExtractionLimits) -> ExtractedText:
    raw_pages, page_count = _EXTRACTORS[kind](path, limits)
    pages = [
        ExtractedPage(page.page_number, text)
        for page in raw_pages
        if (text := normalize_text(page.text))
    ]
    if not pages:
        raise IngestionError(
            "no_text_extracted",
            "No extractable text was found. Scanned documents (OCR) are not supported.",
        )
    return ExtractedText(pages=pages, page_count=page_count)
