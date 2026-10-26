"""user authentication: password hash, active flag, normalized email

Revision ID: 3f1c2a9d7e45
Revises: 128b8af81b05
Create Date: 2026-10-27

Schema:
- users.password_hash VARCHAR(255) NULL: Argon2id PHC string.
- users.is_active BOOLEAN NOT NULL DEFAULT true.
- CHECK (email = lower(btrim(email))): the existing unique constraint on email then also
  rejects case and whitespace variants, whatever writes the row.

Existing (legacy) data, deterministic and non-destructive:
- Every existing user keeps its id, email and documents. No document, chunk, embedding,
  search vector or job is touched or reassigned.
- Existing users get password_hash NULL: such an account cannot log in, and registering
  the same email is refused as a duplicate, so nobody can take it over by signing up.
  An operator who controls the server claims it explicitly:
      python -m app.admin set-password --email <legacy email>
- The upgrade refuses to run if an existing email is not normalized, instead of
  rewriting or merging accounts silently (the ORM always normalized, so none should be).

Downgrade drops the columns and the constraint: password hashes are lost (accounts become
password-less again); documents are untouched.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3f1c2a9d7e45"
down_revision: str | None = "128b8af81b05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Data check needs a live connection; `alembic upgrade --sql` only renders the DDL.
    bad = (
        0
        if op.get_context().as_sql
        else op.get_bind().scalar(
            sa.text("SELECT count(*) FROM users WHERE email <> lower(btrim(email))")
        )
    )
    if bad:
        raise RuntimeError(
            f"{bad} user email(s) are not normalized; fix them by hand before upgrading "
            "(the migration never merges or rewrites accounts)"
        )
    op.add_column("users", sa.Column("password_hash", sa.String(length=255), nullable=True))
    op.add_column(
        "users",
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_users_email_normalized"), "users", "email = lower(btrim(email))"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_users_email_normalized"), "users", type_="check")
    op.drop_column("users", "is_active")
    op.drop_column("users", "password_hash")
