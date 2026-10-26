"""Periodic cleanup run by the indexing worker when idle (bounded table growth).

- rate_limits: counters whose window ended more than a day ago;
- refresh_tokens: tokens expired for more than a day (spent ones stay until then, so a
  replayed token is still recognized as reuse within its lifetime);
- auth_sessions: sessions that ended (expired or revoked) more than 7 days ago, with
  their refresh tokens (cascade). Recent ended sessions are kept for diagnosis.
Every statement is a bounded DELETE on an indexed or primary-key column.
"""

import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services import rate_limit

logger = logging.getLogger(__name__)


def prune(db: Session) -> dict[str, int]:
    removed = {
        "rate_limits": rate_limit.prune(db),
        "refresh_tokens": db.execute(
            text("DELETE FROM refresh_tokens WHERE expires_at < now() - interval '1 day'")
        ).rowcount,
        "auth_sessions": db.execute(
            text(
                "DELETE FROM auth_sessions WHERE coalesce(revoked_at, expires_at) "
                "< now() - interval '7 days' AND (revoked_at IS NOT NULL OR expires_at < now())"
            )
        ).rowcount,
    }
    db.commit()
    logger.info(
        "Maintenance: removed rate_limits=%d refresh_tokens=%d auth_sessions=%d",
        removed["rate_limits"], removed["refresh_tokens"], removed["auth_sessions"],
    )
    return removed
