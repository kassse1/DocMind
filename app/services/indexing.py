"""Indexing use case: re-extract the stored original, chunk, embed, persist.

Transaction boundaries (none is held open during CPU-heavy work):
1. Claim (services/jobs.py): job -> running, document -> processing; committed at once.
2. Extract, chunk and embed outside any transaction. The chunk count is capped, because
   all chunks and vectors of a document are held in memory until step 3.
3. One transaction replaces the document's chunks and marks job and document done, so
   readers see either the old chunk set or the new one, never a mix.
On failure, a separate transaction records the outcome: retry later (document back to
pending, existing chunks untouched) or fail for good (document failed, chunks removed).

Document text is only ever passed to the tokenizer and the embedding model as data;
nothing in it is interpreted. Logs carry ids, counts and error codes only.
"""

import logging
import time
import uuid
from typing import Any

from sqlalchemy import delete, insert
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.embeddings.base import Embedder
from app.ingestion.chunking import Chunk, Chunker
from app.ingestion.errors import IngestionError
from app.ingestion.file_types import kind_for_media_type
from app.ingestion.parsers import ExtractionLimits, extract_text
from app.ingestion.storage import FileStorage
from app.models import Document, DocumentChunk, DocumentStatus
from app.services import jobs
from app.services.jobs import ClaimedJob

logger = logging.getLogger(__name__)

INTERNAL_ERROR_MESSAGE = "Indexing failed due to an internal error."


class IndexingError(Exception):
    """A failure with a stable code and a message that is safe to show to the user."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class DocumentIndexer:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        storage: FileStorage,
        embedder: Embedder,
        settings: Settings,
    ) -> None:
        # Checked up front: otherwise the model would silently truncate long chunks.
        if settings.chunk_size_tokens > embedder.max_input_tokens:
            raise ValueError(
                f"chunk_size_tokens ({settings.chunk_size_tokens}) exceeds the model input "
                f"limit ({embedder.max_input_tokens} tokens)"
            )
        self._session_factory = session_factory
        self._storage = storage
        self._embedder = embedder
        self._settings = settings
        self._chunker = Chunker(
            embedder.tokenizer, settings.chunk_size_tokens, settings.chunk_overlap_tokens
        )

    def run_once(self) -> bool:
        """Claim and process one job. Returns False when no job is runnable."""
        with self._session_factory() as session:
            claimed = jobs.claim_next_job(
                session,
                lease_timeout_s=self._settings.job_lease_timeout_s,
                max_attempts=self._settings.job_max_attempts,
            )
        if claimed is None:
            return False
        self.process(claimed)
        return True

    def process(self, claimed: ClaimedJob) -> None:
        started = time.monotonic()
        logger.info(
            "Indexing started: job=%s document=%s attempt=%d",
            claimed.job_id, claimed.document_id, claimed.attempt,
        )
        try:
            rows = self._build_rows(claimed.document_id)
            committed = self._commit_success(claimed, rows)
        except Exception as exc:
            self._record_failure(claimed, exc)
            return
        if committed:
            logger.info(
                "Indexing finished: job=%s document=%s chunks=%d seconds=%.2f",
                claimed.job_id, claimed.document_id, len(rows), time.monotonic() - started,
            )

    def _build_rows(self, document_id: uuid.UUID) -> list[dict[str, Any]]:
        # Short read in its own session, closed before the heavy work starts.
        with self._session_factory() as session:
            document = session.get(Document, document_id)
            if document is None:
                raise IndexingError("document_missing", "The document no longer exists.")
            content_type = document.content_type

        path = self._storage.path_for(document_id)
        if not path.is_file():
            raise IndexingError("original_missing", "The original file is no longer available.")
        extraction = extract_text(
            kind_for_media_type(content_type),
            path,
            ExtractionLimits(
                max_pdf_pages=self._settings.max_pdf_pages,
                max_docx_uncompressed_bytes=self._settings.max_docx_uncompressed_bytes,
            ),
        )
        chunks = self._collect_chunks((page.page_number, page.text) for page in extraction.pages)

        rows: list[dict[str, Any]] = []
        batch_size = self._settings.embedding_batch_size
        for start in range(0, len(chunks), batch_size):
            batch = chunks[start : start + batch_size]
            vectors = self._embedder.embed_passages([chunk.text for chunk in batch])
            rows.extend(
                {
                    "document_id": document_id,
                    "chunk_index": chunk.chunk_index,
                    "content": chunk.text,
                    "page_number": chunk.page_number,
                    "char_start": chunk.char_start,
                    "char_end": chunk.char_end,
                    "token_count": chunk.token_count,
                    "embedding": vector,
                    "embedding_model": self._embedder.model_name,
                }
                for chunk, vector in zip(batch, vectors, strict=True)
            )
        return rows

    def _collect_chunks(self, pages: Any) -> list[Chunk]:
        limit = self._settings.max_chunks_per_document
        max_tokens = self._embedder.max_input_tokens
        chunks: list[Chunk] = []
        for chunk in self._chunker.chunk_pages(pages):
            if len(chunks) >= limit:
                raise IndexingError(
                    "too_many_chunks",
                    f"The document is too large to index (more than {limit} chunks).",
                )
            if chunk.token_count > max_tokens:
                # Not expected (chunk_size is validated), but never let the model truncate.
                raise IndexingError("chunk_too_long", INTERNAL_ERROR_MESSAGE)
            chunks.append(chunk)
        return chunks

    def _commit_success(self, claimed: ClaimedJob, rows: list[dict[str, Any]]) -> bool:
        with self._session_factory() as session, session.begin():
            job = jobs.lock_owned_job(session, claimed)
            if job is None:
                logger.warning(
                    "Indexing result discarded, job no longer owned: job=%s document=%s",
                    claimed.job_id, claimed.document_id,
                )
                return False
            document = session.get(Document, claimed.document_id, with_for_update=True)
            assert document is not None  # the job row exists, so its document does too
            session.execute(
                delete(DocumentChunk).where(DocumentChunk.document_id == claimed.document_id)
            )
            session.execute(insert(DocumentChunk), rows)
            document.status = DocumentStatus.READY
            document.error_message = None
            jobs.mark_succeeded(job)
        return True

    def _record_failure(self, claimed: ClaimedJob, exc: Exception) -> None:
        if isinstance(exc, IngestionError | IndexingError):
            code, message = exc.code, exc.message
            retryable = isinstance(exc, IndexingError) and exc.retryable
        else:
            # Unexpected (database hiccup, model error...): worth another attempt.
            code, message, retryable = type(exc).__name__, INTERNAL_ERROR_MESSAGE, True
            # Tracebacks only at DEBUG: exception messages from libraries are not vetted.
            logger.debug("Indexing error details: job=%s", claimed.job_id, exc_info=exc)

        will_retry = retryable and claimed.attempt < self._settings.job_max_attempts
        logger.warning(
            "Indexing failed: job=%s document=%s attempt=%d code=%s will_retry=%s",
            claimed.job_id, claimed.document_id, claimed.attempt, code, will_retry,
        )
        try:
            with self._session_factory() as session, session.begin():
                job = jobs.lock_owned_job(session, claimed)
                if job is None:
                    return
                document = session.get(Document, claimed.document_id, with_for_update=True)
                assert document is not None
                if will_retry:
                    jobs.mark_for_retry(job, code, self._settings.job_retry_delay_s)
                    document.status = DocumentStatus.PENDING
                    document.error_message = None
                else:
                    jobs.mark_failed(job, code)
                    jobs.mark_document_failed(session, document, message)
        except Exception as record_exc:
            # The job stays "running"; once its lease expires another claim recovers it.
            logger.error(
                "Could not record indexing failure: job=%s error=%s",
                claimed.job_id, type(record_exc).__name__,
            )
