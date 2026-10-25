"""full-text search vector on document chunks

Revision ID: 128b8af81b05
Revises: 76cdab7f68eb
Create Date: 2026-10-26

- Adds document_chunks.search_vector, a STORED generated column:
  to_tsvector('simple', content) || to_tsvector('russian', content).
  PostgreSQL computes it for every existing row while adding the column and keeps it in
  sync on every insert/update, so existing documents need neither re-upload nor re-index.
- Adds a GIN index on it for the @@ operator.

Adding a stored generated column rewrites the table under an ACCESS EXCLUSIVE lock, and the
index is built non-concurrently: fine at this project's size; on a large production table
build the index with CREATE INDEX CONCURRENTLY outside a transaction instead.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "128b8af81b05"
down_revision: str | None = "76cdab7f68eb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copy of app.models.document.FULL_TEXT_VECTOR_SQL at this revision: a migration must
# not change when the application constant changes later.
SEARCH_VECTOR_SQL = (
    "to_tsvector('simple'::regconfig, content) || to_tsvector('russian'::regconfig, content)"
)


def upgrade() -> None:
    op.add_column(
        "document_chunks",
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed(SEARCH_VECTOR_SQL, persisted=True),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_document_chunks_search_vector",
        "document_chunks",
        ["search_vector"],
        unique=False,
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_document_chunks_search_vector", table_name="document_chunks")
    op.drop_column("document_chunks", "search_vector")
