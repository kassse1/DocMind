"""Indexing pipeline against the real test database, with the fake embedder.

Like the upload tests, these commit for real, so tables are truncated around each test.
"""

import io
import logging
import uuid
from collections.abc import Iterator, Sequence
from pathlib import Path

import numpy as np
import pytest
from fastapi import UploadFile
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.database import create_session_factory
from app.embeddings.fake import FakeEmbedder
from app.ingestion.storage import FileStorage
from app.models import EMBEDDING_DIMENSION, Document, DocumentChunk, DocumentStatus, Job, JobStatus
from app.services import jobs
from app.services.documents import upload_document
from app.services.indexing import INTERNAL_ERROR_MESSAGE, DocumentIndexer
from app.services.users import get_or_create_user
from tests.samples import make_pdf

pytestmark = pytest.mark.integration

TEXT = "\n\n".join(
    [
        "The contract was signed in Almaty on the fifth of March by both parties.",
        "Договор был подписан в Алматы пятого марта обеими сторонами без оговорок.",
        "Келісімшарт наурыздың бесінде Алматыда екі тарап қол қойды, ескертусіз.",
        "Payment is due within thirty days after the invoice has been received.",
    ]
)


def _truncate(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE users CASCADE"))


@pytest.fixture
def settings(test_database_url: str, tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        database_url=test_database_url,
        storage_dir=tmp_path / "storage",
        embedding_backend="fake",
        chunk_size_tokens=16,
        chunk_overlap_tokens=4,
        embedding_batch_size=2,
    )


@pytest.fixture
def session_factory(pg_engine: Engine) -> Iterator[sessionmaker[Session]]:
    _truncate(pg_engine)
    yield create_session_factory(pg_engine)
    _truncate(pg_engine)


@pytest.fixture
def storage(settings: Settings) -> FileStorage:
    return FileStorage(settings.storage_dir)


def _indexer(session_factory, storage, settings, embedder=None) -> DocumentIndexer:
    return DocumentIndexer(
        session_factory, storage, embedder or FakeEmbedder(EMBEDDING_DIMENSION), settings
    )


@pytest.fixture
def indexer(session_factory, storage, settings) -> DocumentIndexer:
    return _indexer(session_factory, storage, settings)


def _upload(session_factory, storage, settings, name="notes.txt", data=None) -> uuid.UUID:
    with session_factory() as session:
        user = get_or_create_user(session, "indexer@example.com")
        upload = UploadFile(io.BytesIO(data or TEXT.encode()), filename=name)
        return upload_document(session, storage, settings, user, upload).document.id


def _document(session_factory, doc_id) -> Document:
    with session_factory() as session:
        return session.get(Document, doc_id)


def _jobs(session_factory, doc_id) -> list[Job]:
    with session_factory() as session:
        return list(
            session.scalars(select(Job).where(Job.document_id == doc_id).order_by(Job.created_at))
        )


def _chunks(session_factory, doc_id) -> list[DocumentChunk]:
    with session_factory() as session:
        return list(
            session.scalars(
                select(DocumentChunk)
                .where(DocumentChunk.document_id == doc_id)
                .order_by(DocumentChunk.chunk_index)
            )
        )


def _execute(session_factory, sql: str, **params) -> None:
    with session_factory() as session, session.begin():
        session.execute(text(sql), params)


class FlakyEmbedder(FakeEmbedder):
    """Raises on the first `failures` calls, then behaves like the fake."""

    def __init__(self, failures: int) -> None:
        super().__init__(EMBEDDING_DIMENSION)
        self.failures = failures

    def embed_passages(self, texts: Sequence[str]) -> np.ndarray:
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("simulated model failure with document text inside")
        return super().embed_passages(texts)


def test_upload_queues_an_indexing_job(session_factory, storage, settings) -> None:
    doc_id = _upload(session_factory, storage, settings)
    assert _document(session_factory, doc_id).status is DocumentStatus.PENDING
    [job] = _jobs(session_factory, doc_id)
    assert (job.status, job.attempts) == (JobStatus.QUEUED, 0)


def test_run_once_indexes_document(session_factory, storage, settings, indexer) -> None:
    doc_id = _upload(session_factory, storage, settings)

    assert indexer.run_once() is True
    assert indexer.run_once() is False  # queue drained

    document = _document(session_factory, doc_id)
    assert (document.status, document.error_message) == (DocumentStatus.READY, None)
    [job] = _jobs(session_factory, doc_id)
    assert (job.status, job.attempts, job.locked_at) == (JobStatus.SUCCEEDED, 1, None)

    chunks = _chunks(session_factory, doc_id)
    # Each paragraph fits the 16-token budget but no two do together: one chunk per
    # paragraph, possibly starting with the previous paragraph's tail as overlap.
    paragraphs = TEXT.split("\n\n")
    assert len(chunks) == len(paragraphs)
    assert all(c.content.endswith(p) for c, p in zip(chunks, paragraphs, strict=True))
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    for chunk in chunks:
        assert TEXT[chunk.char_start : chunk.char_end] == chunk.content
        assert chunk.page_number is None
        assert 0 < chunk.token_count <= settings.chunk_size_tokens
        assert chunk.embedding_model == "fake/sha256"
        assert np.linalg.norm(chunk.embedding) == pytest.approx(1.0, rel=1e-5)


def test_long_paragraph_is_split_with_overlap(session_factory, storage, settings, indexer) -> None:
    words = [f"сөз{i}" for i in range(60)]
    doc_id = _upload(session_factory, storage, settings, data=" ".join(words).encode())

    indexer.run_once()

    chunks = _chunks(session_factory, doc_id)
    assert len(chunks) > 3
    for prev, nxt in zip(chunks, chunks[1:], strict=False):
        assert prev.content.split()[-4:] == nxt.content.split()[:4]


def test_pdf_chunks_keep_page_numbers(session_factory, storage, settings, indexer) -> None:
    pdf = make_pdf(["Page one says hello.", "", "Page three says goodbye."])
    doc_id = _upload(session_factory, storage, settings, "report.pdf", pdf)

    indexer.run_once()

    chunks = _chunks(session_factory, doc_id)
    assert [(c.page_number, c.content) for c in chunks] == [
        (1, "Page one says hello."),
        (3, "Page three says goodbye."),
    ]


def test_reindex_replaces_chunks_without_duplicates(
    session_factory, storage, settings, indexer
) -> None:
    doc_id = _upload(session_factory, storage, settings)
    indexer.run_once()
    first = _chunks(session_factory, doc_id)

    with session_factory() as session, session.begin():
        assert jobs.request_reindex(session, doc_id) is True
        # A second request while one is queued is a no-op (partial unique index).
        assert jobs.request_reindex(session, doc_id) is False
    assert _document(session_factory, doc_id).status is DocumentStatus.PENDING

    indexer.run_once()

    second = _chunks(session_factory, doc_id)
    assert [c.content for c in second] == [c.content for c in first]
    assert {c.id for c in second}.isdisjoint({c.id for c in first})
    assert _document(session_factory, doc_id).status is DocumentStatus.READY
    assert [j.status for j in _jobs(session_factory, doc_id)] == [JobStatus.SUCCEEDED] * 2


def test_failed_write_rolls_back_and_keeps_previous_chunks(
    session_factory, storage, settings, indexer, monkeypatch
) -> None:
    doc_id = _upload(session_factory, storage, settings)
    indexer.run_once()
    before = {c.id for c in _chunks(session_factory, doc_id)}
    with session_factory() as session, session.begin():
        jobs.request_reindex(session, doc_id)

    # The DELETE of old chunks succeeds, then the INSERT violates a CHECK constraint.
    original = indexer._build_rows

    def broken_rows(document_id):
        rows = original(document_id)
        rows[-1]["char_start"] = -1
        return rows

    monkeypatch.setattr(indexer, "_build_rows", broken_rows)
    indexer.run_once()

    assert {c.id for c in _chunks(session_factory, doc_id)} == before
    document = _document(session_factory, doc_id)
    assert (document.status, document.error_message) == (DocumentStatus.PENDING, None)
    job = _jobs(session_factory, doc_id)[-1]
    assert (job.status, job.last_error) == (JobStatus.QUEUED, "IntegrityError")


def test_transient_failure_is_retried_with_backoff(session_factory, storage, settings) -> None:
    indexer = _indexer(session_factory, storage, settings, FlakyEmbedder(failures=1))
    doc_id = _upload(session_factory, storage, settings)

    indexer.run_once()

    [job] = _jobs(session_factory, doc_id)
    assert (job.status, job.attempts, job.last_error) == (JobStatus.QUEUED, 1, "RuntimeError")
    assert _document(session_factory, doc_id).status is DocumentStatus.PENDING
    assert _chunks(session_factory, doc_id) == []
    # Backoff: not runnable yet.
    assert indexer.run_once() is False

    _execute(session_factory, "UPDATE jobs SET run_after = now() - interval '1 second'")
    assert indexer.run_once() is True
    assert _document(session_factory, doc_id).status is DocumentStatus.READY
    assert _jobs(session_factory, doc_id)[0].attempts == 2


def test_document_fails_after_max_attempts(session_factory, storage, settings) -> None:
    indexer = _indexer(session_factory, storage, settings, FlakyEmbedder(failures=99))
    doc_id = _upload(session_factory, storage, settings)

    for _ in range(settings.job_max_attempts):
        _execute(session_factory, "UPDATE jobs SET run_after = now() - interval '1 second'")
        assert indexer.run_once() is True

    document = _document(session_factory, doc_id)
    assert document.status is DocumentStatus.FAILED
    # Safe generic message: the exception text (which could contain document text) is not stored.
    assert document.error_message == INTERNAL_ERROR_MESSAGE
    [job] = _jobs(session_factory, doc_id)
    assert (job.status, job.attempts) == (JobStatus.FAILED, settings.job_max_attempts)
    assert indexer.run_once() is False


def test_permanent_failure_is_not_retried(session_factory, storage, settings, indexer) -> None:
    doc_id = _upload(session_factory, storage, settings)
    storage.delete(doc_id)  # the original disappears from storage

    indexer.run_once()

    document = _document(session_factory, doc_id)
    assert document.status is DocumentStatus.FAILED
    assert document.error_message == "The original file is no longer available."
    [job] = _jobs(session_factory, doc_id)
    assert (job.status, job.attempts, job.last_error) == (JobStatus.FAILED, 1, "original_missing")


def test_failed_reindex_removes_stale_chunks(session_factory, storage, settings, indexer) -> None:
    doc_id = _upload(session_factory, storage, settings)
    indexer.run_once()
    with session_factory() as session, session.begin():
        jobs.request_reindex(session, doc_id)
    storage.delete(doc_id)

    indexer.run_once()

    assert _document(session_factory, doc_id).status is DocumentStatus.FAILED
    assert _chunks(session_factory, doc_id) == []


def test_too_many_chunks_fails_cleanly(session_factory, storage, settings) -> None:
    limited = settings.model_copy(update={"max_chunks_per_document": 2})
    doc_id = _upload(session_factory, storage, settings)

    _indexer(session_factory, storage, limited).run_once()

    document = _document(session_factory, doc_id)
    assert document.status is DocumentStatus.FAILED
    assert "more than 2 chunks" in document.error_message
    assert _chunks(session_factory, doc_id) == []


def test_running_job_with_live_lease_is_not_claimed(session_factory, storage, settings) -> None:
    doc_id = _upload(session_factory, storage, settings)
    with session_factory() as session:
        claimed = jobs.claim_next_job(session, lease_timeout_s=60, max_attempts=3)
    assert claimed is not None and claimed.attempt == 1
    assert _document(session_factory, doc_id).status is DocumentStatus.PROCESSING

    with session_factory() as session:
        assert jobs.claim_next_job(session, lease_timeout_s=60, max_attempts=3) is None


def test_expired_lease_is_reclaimed(session_factory, storage, settings, indexer) -> None:
    doc_id = _upload(session_factory, storage, settings)
    with session_factory() as session:
        jobs.claim_next_job(session, lease_timeout_s=60, max_attempts=3)
    # The worker that claimed it "died" two hours ago.
    _execute(session_factory, "UPDATE jobs SET locked_at = now() - interval '2 hours'")

    assert indexer.run_once() is True

    assert _document(session_factory, doc_id).status is DocumentStatus.READY
    assert _jobs(session_factory, doc_id)[0].attempts == 2


def test_job_abandoned_after_max_attempts_is_failed(session_factory, storage, settings) -> None:
    doc_id = _upload(session_factory, storage, settings)
    _execute(
        session_factory,
        "UPDATE jobs SET status = 'running', attempts = :n, locked_at = now() - interval '2 hours'",
        n=settings.job_max_attempts,
    )

    assert _indexer(session_factory, storage, settings).run_once() is False

    document = _document(session_factory, doc_id)
    assert (document.status, document.error_message) == (
        DocumentStatus.FAILED,
        jobs.ABANDONED_MESSAGE,
    )
    assert _jobs(session_factory, doc_id)[0].last_error == "lease_expired"


def test_result_is_discarded_when_lease_was_lost(
    session_factory, storage, settings, indexer
) -> None:
    doc_id = _upload(session_factory, storage, settings)
    with session_factory() as session:
        claimed = jobs.claim_next_job(session, lease_timeout_s=60, max_attempts=3)
    # Meanwhile another worker reclaimed the job (its attempt number moved on).
    _execute(session_factory, "UPDATE jobs SET attempts = attempts + 1")

    indexer.process(claimed)

    assert _chunks(session_factory, doc_id) == []
    [job] = _jobs(session_factory, doc_id)
    assert (job.status, job.attempts) == (JobStatus.RUNNING, 2)


def test_locked_job_is_skipped_not_waited_for(
    session_factory, storage, settings, indexer, pg_engine
) -> None:
    _upload(session_factory, storage, settings)
    with pg_engine.connect() as conn:
        transaction = conn.begin()
        conn.execute(text("SELECT id FROM jobs FOR UPDATE"))
        # Another worker holds the row lock: SKIP LOCKED returns nothing instead of blocking.
        assert indexer.run_once() is False
        transaction.rollback()
    assert indexer.run_once() is True


def test_chunk_size_above_model_limit_is_rejected(session_factory, storage, settings) -> None:
    with pytest.raises(ValueError, match="exceeds the model input limit"):
        DocumentIndexer(
            session_factory,
            storage,
            FakeEmbedder(EMBEDDING_DIMENSION, max_input_tokens=8),
            settings,
        )


def test_logs_never_contain_document_text_or_filename(
    session_factory, storage, settings, caplog
) -> None:
    secret = "TOPSECRETWORD"
    data = f"{secret} appears here.\n\nAnd {secret} again in another paragraph.".encode()
    indexer = _indexer(session_factory, storage, settings, FlakyEmbedder(failures=1))
    caplog.set_level(logging.DEBUG)

    _upload(session_factory, storage, settings, "private-name.txt", data)
    indexer.run_once()
    _execute(session_factory, "UPDATE jobs SET run_after = now() - interval '1 second'")
    indexer.run_once()

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "Indexing finished" in messages
    assert secret not in messages
    assert "private-name" not in messages
