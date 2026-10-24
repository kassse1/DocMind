"""Importing this package registers every model on Base.metadata (used by Alembic)."""

from app.models.base import Base
from app.models.conversation import Conversation, Message, MessageRole
from app.models.document import EMBEDDING_DIMENSION, Document, DocumentChunk, DocumentStatus
from app.models.job import ACTIVE_JOB_STATUSES, Job, JobKind, JobStatus
from app.models.user import User

__all__ = [
    "ACTIVE_JOB_STATUSES",
    "EMBEDDING_DIMENSION",
    "Base",
    "Conversation",
    "Document",
    "DocumentChunk",
    "DocumentStatus",
    "Job",
    "JobKind",
    "JobStatus",
    "Message",
    "MessageRole",
    "User",
]
