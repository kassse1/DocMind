"""Upload use case: validate, extract, persist metadata, store the original file, and
queue an indexing job in the same transaction (the worker does the heavy part).

Consistency rule: a database row is committed only after its file is in place, and the
file is removed again if the commit fails. A crash between the two steps can leave an
orphaned file (harmless, collectable later) but never a row pointing at a missing file.
"""

import hashlib
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from fastapi import UploadFile, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError
from app.ingestion.errors import IngestionError
from app.ingestion.file_types import (
    HEADER_BYTES,
    resolve_file_type,
    sanitize_filename,
    verify_magic_bytes,
)
from app.ingestion.parsers import ExtractedText, ExtractionLimits, extract_text
from app.ingestion.storage import FileStorage
from app.models import Document, User
from app.services.jobs import enqueue_indexing

logger = logging.getLogger(__name__)

_COPY_CHUNK_BYTES = 1024 * 1024
_DUPLICATE_CONSTRAINT = "uq_documents_user_id_content_hash"

# HTTP status for each ingestion error code; anything unlisted is a 422.
_STATUS_BY_CODE = {
    "unsupported_file_type": status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
    "content_type_mismatch": status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
    "empty_file": status.HTTP_400_BAD_REQUEST,
    "file_too_large": status.HTTP_413_CONTENT_TOO_LARGE,
}


@dataclass(frozen=True)
class UploadResult:
    document: Document
    extraction: ExtractedText


def _copy_bounded(source: BinaryIO, destination: Path, max_bytes: int) -> tuple[int, str]:
    """Copy in fixed-size chunks, hashing on the way; stop as soon as the limit is crossed."""
    digest = hashlib.sha256()
    size = 0
    with destination.open("wb") as out:
        while chunk := source.read(_COPY_CHUNK_BYTES):
            size += len(chunk)
            if size > max_bytes:
                raise IngestionError(
                    "file_too_large",
                    "The file exceeds the upload size limit.",
                    {"max_upload_bytes": max_bytes},
                )
            digest.update(chunk)
            out.write(chunk)
    return size, digest.hexdigest()


def _read_header(path: Path) -> bytes:
    with path.open("rb") as f:
        return f.read(HEADER_BYTES)


def _duplicate_error(existing_id: uuid.UUID | None) -> AppError:
    return AppError(
        "duplicate_document",
        "This file has already been uploaded.",
        status.HTTP_409_CONFLICT,
        details={"document_id": str(existing_id) if existing_id else None},
    )


def _find_duplicate(session: Session, user_id: uuid.UUID, content_hash: str) -> uuid.UUID | None:
    return session.scalar(
        select(Document.id).where(
            Document.user_id == user_id, Document.content_hash == content_hash
        )
    )


def _is_duplicate_violation(exc: IntegrityError) -> bool:
    diag = getattr(exc.orig, "diag", None)
    return getattr(diag, "constraint_name", None) == _DUPLICATE_CONSTRAINT


def upload_document(
    session: Session,
    storage: FileStorage,
    settings: Settings,
    user: User,
    upload: UploadFile,
) -> UploadResult:
    try:
        return _upload(session, storage, settings, user, upload)
    except IngestionError as exc:
        # Log the reason code only: never the filename or file contents.
        logger.info("Upload rejected: user=%s code=%s", user.id, exc.code)
        raise AppError(
            exc.code,
            exc.message,
            _STATUS_BY_CODE.get(exc.code, status.HTTP_422_UNPROCESSABLE_CONTENT),
            details=exc.details,
        ) from None


def _upload(
    session: Session,
    storage: FileStorage,
    settings: Settings,
    user: User,
    upload: UploadFile,
) -> UploadResult:
    filename = sanitize_filename(upload.filename)
    spec = resolve_file_type(filename, upload.content_type)

    temp_path = storage.new_temp_path()
    try:
        size, content_hash = _copy_bounded(upload.file, temp_path, settings.max_upload_bytes)
        if size == 0:
            raise IngestionError("empty_file", "The uploaded file is empty.")
        verify_magic_bytes(spec, _read_header(temp_path))

        # Cheap duplicate check before the expensive parse.
        if (existing := _find_duplicate(session, user.id, content_hash)) is not None:
            raise _duplicate_error(existing)

        extraction = extract_text(
            spec.kind,
            temp_path,
            ExtractionLimits(
                max_pdf_pages=settings.max_pdf_pages,
                max_docx_uncompressed_bytes=settings.max_docx_uncompressed_bytes,
            ),
        )

        document = Document(
            user_id=user.id,
            filename=filename,
            content_type=spec.media_type,
            file_size=size,
            content_hash=content_hash,
        )
        session.add(document)
        try:
            session.flush()
        except IntegrityError as exc:
            session.rollback()
            if not _is_duplicate_violation(exc):
                raise
            # A concurrent request inserted the same file between our check and flush.
            raise _duplicate_error(_find_duplicate(session, user.id, content_hash)) from None
        # Same transaction as the document row: every committed upload has a job.
        enqueue_indexing(session, document.id)

        storage.store(temp_path, document.id)
        try:
            session.commit()
        except Exception:
            session.rollback()
            storage.remove_quietly(storage.path_for(document.id))
            raise
    finally:
        storage.remove_quietly(temp_path)

    logger.info(
        "Document uploaded: id=%s user=%s type=%s size=%d", document.id, user.id, spec.kind, size
    )
    return UploadResult(document=document, extraction=extraction)


def _not_found() -> AppError:
    # Same response for a missing document and another user's: existence is not disclosed.
    return AppError("document_not_found", "Document not found.", status.HTTP_404_NOT_FOUND)


def list_documents(session: Session, user: User, limit: int, offset: int) -> list[Document]:
    """The caller's documents only, newest first."""
    return list(
        session.scalars(
            select(Document)
            .where(Document.user_id == user.id)
            .order_by(Document.created_at.desc(), Document.id)
            .limit(limit)
            .offset(offset)
        )
    )


def get_document(session: Session, user: User, document_id: uuid.UUID) -> Document:
    # Ownership is part of the query: another user's row is never loaded.
    document = session.scalars(
        select(Document).where(Document.id == document_id, Document.user_id == user.id)
    ).one_or_none()
    if document is None:
        raise _not_found()
    return document


def delete_document(
    session: Session, storage: FileStorage, user: User, document_id: uuid.UUID
) -> None:
    """Delete the row (chunks, embeddings and jobs cascade in PostgreSQL), then the file.

    Row first: if removing the file fails, an orphaned file remains (harmless, unreachable)
    rather than a row pointing at a missing file. A worker indexing the document at the
    same time loses its job row and discards its results (app/services/jobs.py).
    """
    document = get_document(session, user, document_id)
    session.delete(document)
    session.commit()
    try:
        storage.delete(document_id)
    except OSError as exc:
        logger.warning("Stored file of a deleted document not removed (%s)", type(exc).__name__)
    logger.info("Deleted document=%s user=%s", document_id, user.id)
