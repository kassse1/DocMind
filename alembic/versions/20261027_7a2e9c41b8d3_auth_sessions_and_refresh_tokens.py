"""auth sessions and refresh tokens

Revision ID: 7a2e9c41b8d3
Revises: 3f1c2a9d7e45
Create Date: 2026-10-27

New tables only; no existing row changes:
- auth_sessions: one per login (refresh-token family), absolute expiry, revocation.
- refresh_tokens: SHA-256 hashes of opaque refresh tokens, unique, with used_at for
  rotation and reuse detection.
Both cascade from users. Access tokens issued before this revision carry no session id and
are rejected after the upgrade (users log in again), which at most costs one login.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7a2e9c41b8d3"
down_revision: str | None = "3f1c2a9d7e45"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "auth_sessions",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(length=32), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_auth_sessions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_sessions")),
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"], unique=False)
    op.create_table(
        "refresh_tokens",
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["auth_sessions.id"],
            name=op.f("fk_refresh_tokens_session_id_auth_sessions"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_refresh_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_refresh_tokens_token_hash")),
    )
    op.create_index(
        "ix_refresh_tokens_session_id", "refresh_tokens", ["session_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_refresh_tokens_session_id", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
    op.drop_index("ix_auth_sessions_user_id", table_name="auth_sessions")
    op.drop_table("auth_sessions")
