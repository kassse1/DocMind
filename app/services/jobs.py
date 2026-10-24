"""PostgreSQL job queue: enqueue, claim with FOR UPDATE SKIP LOCKED, and fence results.

Lock order is always job row, then document row, so concurrent claims, completions and
re-index requests cannot deadlock each other.
"""

import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import and_, delete, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import Document, DocumentChunk, DocumentStatus, Job, JobKind, JobStatus

_ACTIVE_PREDICATE = text("status IN ('queued', 'running')")
ABANDONED_MESSAGE = "Indexing did not complete after repeated attempts."


@dataclass(frozen=True)
class ClaimedJob:
    job_id: uuid.UUID
    document_id: uuid.UUID
    # Fencing token: the attempt number this worker owns.
    attempt: int


def enqueue_indexing(session: Session, document_id: uuid.UUID) -> bool:
    """Queue an indexing job unless one is already queued or running. Caller commits."""
    statement = (
        insert(Job)
        .values(id=uuid.uuid4(), document_id=document_id, kind=JobKind.INDEX_DOCUMENT)
        .on_conflict_do_nothing(index_elements=["document_id"], index_where=_ACTIVE_PREDICATE)
        .returning(Job.id)
    )
    return session.execute(statement).scalar_one_or_none() is not None


def request_reindex(session: Session, document_id: uuid.UUID) -> bool:
    """Queue a re-index and mark the document pending. Existing chunks are kept until the
    new set replaces them in one transaction. Caller commits."""
    if not enqueue_indexing(session, document_id):
        return False
    document = session.get(Document, document_id, with_for_update=True)
    if document is not None:
        document.status = DocumentStatus.PENDING
        document.error_message = None
    return True


def claim_next_job(
    session: Session, *, lease_timeout_s: int, max_attempts: int
) -> ClaimedJob | None:
    """Claim one runnable job and commit at once, so the claim is visible to the API."""
    lease_expired = and_(
        Job.status == JobStatus.RUNNING,
        Job.locked_at < func.now() - timedelta(seconds=lease_timeout_s),
    )
    runnable = and_(Job.status == JobStatus.QUEUED, Job.run_after <= func.now())
    query = (
        select(Job)
        .where(Job.kind == JobKind.INDEX_DOCUMENT, or_(runnable, lease_expired))
        .order_by(Job.run_after, Job.created_at)
        .limit(1)
        # Other workers skip this row instead of waiting for it.
        .with_for_update(skip_locked=True)
    )
    while True:
        with session.begin():
            job = session.scalars(query).first()
            if job is None:
                return None
            document = session.get(Document, job.document_id, with_for_update=True)
            assert document is not None  # FK + ON DELETE CASCADE: a job implies its document
            if job.attempts >= max_attempts:
                # Every attempt died without recording a result (killed or crashed worker).
                job.status = JobStatus.FAILED
                job.locked_at = None
                job.last_error = "lease_expired"
                mark_document_failed(session, document, ABANDONED_MESSAGE)
                continue
            job.status = JobStatus.RUNNING
            job.attempts += 1
            job.locked_at = func.now()
            document.status = DocumentStatus.PROCESSING
            document.error_message = None
            claimed = ClaimedJob(job.id, job.document_id, job.attempts)
        return claimed


def lock_owned_job(session: Session, claimed: ClaimedJob) -> Job | None:
    """Lock the job if this worker still owns it. None means the lease was lost (another
    worker reclaimed it) or the document was deleted; the caller must discard its results."""
    job = session.get(Job, claimed.job_id, with_for_update=True, populate_existing=True)
    if job is None or job.status != JobStatus.RUNNING or job.attempts != claimed.attempt:
        return None
    return job


def mark_succeeded(job: Job) -> None:
    job.status = JobStatus.SUCCEEDED
    job.locked_at = None
    job.last_error = None


def mark_for_retry(job: Job, error_code: str, delay_s: int) -> None:
    job.status = JobStatus.QUEUED
    job.locked_at = None
    job.last_error = error_code[:255]
    job.run_after = func.now() + timedelta(seconds=delay_s * job.attempts)


def mark_failed(job: Job, error_code: str) -> None:
    job.status = JobStatus.FAILED
    job.locked_at = None
    job.last_error = error_code[:255]


def mark_document_failed(session: Session, document: Document, message: str) -> None:
    """Failed documents keep no chunks, so nothing stale can be retrieved for them."""
    session.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document.id))
    document.status = DocumentStatus.FAILED
    document.error_message = message
