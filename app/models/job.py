import enum
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, enum_values


class JobKind(enum.StrEnum):
    INDEX_DOCUMENT = "index_document"


class JobStatus(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


ACTIVE_JOB_STATUSES = (JobStatus.QUEUED, JobStatus.RUNNING)


class Job(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Background work item, claimed by worker processes with FOR UPDATE SKIP LOCKED."""

    __tablename__ = "jobs"
    __table_args__ = (
        # At most one queued/running job per document, so enqueueing twice is a no-op.
        Index(
            "uq_jobs_document_id_active",
            "document_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
        # Serves the claim query: runnable jobs in run_after order.
        Index("ix_jobs_status_run_after", "status", "run_after"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    kind: Mapped[JobKind] = mapped_column(
        Enum(
            JobKind,
            name="kind",
            native_enum=False,
            create_constraint=True,
            length=32,
            values_callable=enum_values,
        )
    )
    status: Mapped[JobStatus] = mapped_column(
        Enum(
            JobStatus,
            name="status",
            native_enum=False,
            create_constraint=True,
            length=20,
            values_callable=enum_values,
        ),
        default=JobStatus.QUEUED,
        server_default=JobStatus.QUEUED.value,
    )
    # Incremented on every claim; also serves as a fencing token (see services/jobs.py).
    attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    # Not runnable before this time (retry backoff).
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # When the current attempt was claimed; a running job older than the lease is reclaimable.
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Error code or exception class name only; never document text.
    last_error: Mapped[str | None] = mapped_column(String(255))
