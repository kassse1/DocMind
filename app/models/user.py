from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.conversation import Conversation
    from app.models.document import Document


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        # Normalization enforced by the database too, so the unique constraint on email
        # also rejects case variants written by raw SQL (A@x.com vs a@x.com).
        CheckConstraint("email = lower(btrim(email))", name="email_normalized"),
    )

    # 320 = RFC 5321 maximum address length.
    email: Mapped[str] = mapped_column(String(320), unique=True)
    # Argon2id PHC string (app/core/security.py). NULL = the account cannot log in: legacy
    # accounts created before authentication existed, until an operator sets a password
    # (python -m app.admin set-password). Never returned by the API or logged.
    password_hash: Mapped[str | None] = mapped_column(String(255))
    # Inactive accounts cannot log in and their existing tokens stop working.
    is_active: Mapped[bool] = mapped_column(default=True, server_default=text("true"))

    # passive_deletes=True: let ON DELETE CASCADE in PostgreSQL remove children instead of
    # the ORM loading every chunk into memory just to delete it.
    documents: Mapped[list["Document"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )
    conversations: Mapped[list["Conversation"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )

    @validates("email")
    def _normalize_email(self, _key: str, value: str) -> str:
        # Normalize so the unique constraint also catches case variants (A@x.com vs a@x.com).
        return value.strip().lower()
