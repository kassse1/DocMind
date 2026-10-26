import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.user import User


class AuthSession(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """One login: the family of refresh tokens rotated from it. Access tokens carry its id
    ("sid"), and every authenticated request checks it is still active, so revoking the
    session (logout, detected refresh-token reuse, deactivation, password change) also
    rejects its access tokens at once."""

    __tablename__ = "auth_sessions"
    __table_args__ = (Index("ix_auth_sessions_user_id", "user_id"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # Absolute end of the session: refreshing never extends it past this.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # logout | logout_all | reuse_detected | password_changed | deactivated
    revoked_reason: Mapped[str | None] = mapped_column(String(32))

    user: Mapped["User"] = relationship()
    refresh_tokens: Mapped[list["RefreshToken"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", passive_deletes=True
    )


class RefreshToken(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """A refresh token, stored only as the SHA-256 of its random value. A plain hash is
    enough here (unlike passwords): the value is 256 bits from the OS CSPRNG, so it cannot
    be guessed or brute-forced from the hash. used_at set = already rotated; presenting it
    again is reuse."""

    __tablename__ = "refresh_tokens"
    __table_args__ = (Index("ix_refresh_tokens_session_id", "session_id"),)

    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("auth_sessions.id", ondelete="CASCADE")
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    session: Mapped[AuthSession] = relationship(back_populates="refresh_tokens")
