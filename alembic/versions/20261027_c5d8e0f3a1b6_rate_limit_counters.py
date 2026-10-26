"""rate limit counters

Revision ID: c5d8e0f3a1b6
Revises: 7a2e9c41b8d3
Create Date: 2026-10-27

New table rate_limits (fixed-window counters keyed by a SHA-256 hash); no existing data
changes. Safe to downgrade: counters are transient.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c5d8e0f3a1b6"
down_revision: str | None = "7a2e9c41b8d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rate_limits",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_rate_limits")),
    )
    op.create_index("ix_rate_limits_window_start", "rate_limits", ["window_start"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_rate_limits_window_start", table_name="rate_limits")
    op.drop_table("rate_limits")
