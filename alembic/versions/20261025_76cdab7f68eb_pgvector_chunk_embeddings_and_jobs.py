"""pgvector, chunk embeddings and offsets, jobs table

Revision ID: 76cdab7f68eb
Revises: fead9350b593
Create Date: 2026-10-25

- Enables the pgvector extension (needs a role allowed to CREATE EXTENSION; the Docker
  Compose superuser is). The pgvector/pgvector image ships the extension files.
- document_chunks gains NOT NULL embedding/offset columns. Existing chunk rows are
  deleted first: chunks are derived data that re-indexing recreates, and no released
  code path wrote any before this revision.
- Adds the jobs table and enqueues an indexing job for every pending document, so files
  uploaded before this revision get indexed too.

The vector size (384) belongs to intfloat/multilingual-e5-small. Changing the model to
one with another size requires a new migration and a full re-index.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "76cdab7f68eb"
down_revision: str | None = "fead9350b593"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_DIMENSION = 384


def _timestamp(name: str) -> sa.Column:
    return sa.Column(
        name, sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.execute("DELETE FROM document_chunks")
    op.add_column("document_chunks", sa.Column("char_start", sa.Integer(), nullable=False))
    op.add_column("document_chunks", sa.Column("char_end", sa.Integer(), nullable=False))
    op.add_column("document_chunks", sa.Column("token_count", sa.Integer(), nullable=False))
    op.add_column(
        "document_chunks",
        sa.Column("embedding", Vector(EMBEDDING_DIMENSION), nullable=False),
    )
    op.add_column(
        "document_chunks", sa.Column("embedding_model", sa.String(length=255), nullable=False)
    )
    op.create_check_constraint(
        op.f("ck_document_chunks_char_range_valid"),
        "document_chunks",
        "char_start >= 0 AND char_end > char_start",
    )
    op.create_check_constraint(
        op.f("ck_document_chunks_token_count_non_negative"),
        "document_chunks",
        "token_count >= 0",
    )

    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="queued", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        _timestamp("run_after"),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=255), nullable=True),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_jobs_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("kind IN ('index_document')", name=op.f("ck_jobs_kind")),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')", name=op.f("ck_jobs_status")
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_jobs_attempts_non_negative")),
    )
    op.create_index(
        "uq_jobs_document_id_active",
        "jobs",
        ["document_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_index("ix_jobs_status_run_after", "jobs", ["status", "run_after"], unique=False)

    # gen_random_uuid() is built into PostgreSQL 13+.
    op.execute(
        "INSERT INTO jobs (id, document_id, kind) "
        "SELECT gen_random_uuid(), id, 'index_document' FROM documents WHERE status = 'pending'"
    )


def downgrade() -> None:
    op.drop_index("ix_jobs_status_run_after", table_name="jobs")
    op.drop_index("uq_jobs_document_id_active", table_name="jobs")
    op.drop_table("jobs")

    op.drop_constraint(
        op.f("ck_document_chunks_token_count_non_negative"), "document_chunks", type_="check"
    )
    op.drop_constraint(
        op.f("ck_document_chunks_char_range_valid"), "document_chunks", type_="check"
    )
    for column in ("embedding_model", "embedding", "token_count", "char_end", "char_start"):
        op.drop_column("document_chunks", column)

    op.execute("DROP EXTENSION IF EXISTS vector")
