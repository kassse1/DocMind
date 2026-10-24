from typing import TYPE_CHECKING

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.conversation import Conversation
    from app.models.document import Document


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    # 320 = RFC 5321 maximum address length. No password column: auth is designed in Phase 8.
    email: Mapped[str] = mapped_column(String(320), unique=True)

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
