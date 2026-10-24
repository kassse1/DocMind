import enum
import uuid
from typing import TYPE_CHECKING, Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import CheckConstraint, Enum, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import (
    Base,
    CreatedAtMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    enum_values,
)

if TYPE_CHECKING:
    from app.models.user import User


# Size of document_chunks.embedding. It must match the configured embedding model
# (multilingual-e5-small); switching to a model with another size needs a migration.
EMBEDDING_DIMENSION = 384


class DocumentStatus(enum.StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class Document(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Metadata about an uploaded file. The bytes live in file storage, never in PostgreSQL."""

    __tablename__ = "documents"
    __table_args__ = (
        # Same file uploaded twice by the same user is a duplicate (SHA-256 of the bytes).
        UniqueConstraint("user_id", "content_hash"),
        Index("ix_documents_user_id_created_at", "user_id", "created_at"),
        CheckConstraint("file_size >= 0", name="file_size_non_negative"),
        CheckConstraint("char_length(content_hash) = 64", name="content_hash_sha256_hex"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # Original client filename, kept for display only; storage uses server-generated names.
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(127))
    file_size: Mapped[int]
    content_hash: Mapped[str] = mapped_column(String(64))
    # VARCHAR + CHECK instead of a native PG ENUM: adding a status later is a plain
    # constraint swap in a migration, while ALTER TYPE on native enums is awkward.
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(
            DocumentStatus,
            name="status",
            native_enum=False,
            create_constraint=True,
            length=20,
            values_callable=enum_values,
        ),
        default=DocumentStatus.PENDING,
        server_default=DocumentStatus.PENDING.value,
    )
    # Safe, user-facing failure reason only; never document contents or stack traces.
    error_message: Mapped[str | None] = mapped_column(Text)

    user: Mapped["User"] = relationship(back_populates="documents")
    chunks: Mapped[list["DocumentChunk"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="DocumentChunk.chunk_index",
    )


class DocumentChunk(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "document_chunks"
    __table_args__ = (
        # Also serves as the index for "chunks of document X in order".
        UniqueConstraint("document_id", "chunk_index"),
        CheckConstraint("chunk_index >= 0", name="chunk_index_non_negative"),
        CheckConstraint("page_number IS NULL OR page_number >= 1", name="page_number_positive"),
        CheckConstraint("char_start >= 0 AND char_end > char_start", name="char_range_valid"),
        CheckConstraint("token_count >= 0", name="token_count_non_negative"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    chunk_index: Mapped[int]
    content: Mapped[str] = mapped_column(Text)
    # NULL for formats without pages (TXT, DOCX).
    page_number: Mapped[int | None]
    # Offsets into the normalized text of the page (or of the whole document if it has no
    # pages): content == page_text[char_start:char_end].
    char_start: Mapped[int]
    char_end: Mapped[int]
    token_count: Mapped[int]
    # Unit-length vector. NOT NULL: chunks are written only together with their embeddings.
    # Read back as list[float]; writes accept lists or numpy arrays.
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSION))
    # Vectors of different models are not comparable even at equal dimension,
    # so search must filter on the model that embedded the query.
    embedding_model: Mapped[str] = mapped_column(String(255))
    # "metadata" is reserved on declarative classes, so the attribute is named differently.
    chunk_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, default=dict, server_default="{}"
    )

    document: Mapped["Document"] = relationship(back_populates="chunks")
