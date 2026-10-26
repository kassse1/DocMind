from datetime import datetime

from sqlalchemy import DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class RateLimitCounter(Base):
    """Fixed-window counter (app/services/rate_limit.py). key = SHA-256 hex of
    "scope:identifier": no email or address is stored in clear."""

    __tablename__ = "rate_limits"
    __table_args__ = (Index("ix_rate_limits_window_start", "window_start"),)

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    count: Mapped[int]
