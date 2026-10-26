"""Fixed-window rate limits shared by every API process, stored in PostgreSQL.

One row per (scope, identifier): the SHA-256 of "scope:identifier" (so no email or address
is stored in clear), the start of the current window and a counter. A single
INSERT ... ON CONFLICT DO UPDATE ... RETURNING increments atomically, so concurrent
requests in several processes never lose counts and need no extra locking. The window
starts are computed from the database clock, so all processes agree. Rows are bounded by
the number of distinct clients per window and pruned by the worker (prune()).

Why not Redis: PostgreSQL is already a hard dependency, the limited endpoints (login,
registration, refresh) are low-volume and already write to it, and one upsert per request
is cheap. A fixed window allows up to twice the limit across a window boundary; that is
acceptable for slowing down password guessing, not for precise quotas.

What it does not do: stop a distributed attack from many addresses (the per-email limit
on failed logins only slows guessing one account) or protect endpoints that are not
listed. Put a reverse proxy with its own limits in front of any public deployment.
"""

import hashlib
import logging
from dataclasses import dataclass

from fastapi import status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.errors import AppError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Limit:
    max_requests: int
    window_seconds: int

    @classmethod
    def parse(cls, spec: str) -> "Limit":
        """"20/60" = 20 requests per 60 seconds."""
        count, _, seconds = spec.partition("/")
        limit = cls(int(count), int(seconds))
        if limit.max_requests < 1 or limit.window_seconds < 1:
            raise ValueError(f"invalid rate limit {spec!r}")
        return limit


@dataclass(frozen=True)
class Usage:
    count: int
    retry_after_seconds: int


def _key(scope: str, identifier: str) -> str:
    # Identifiers are bounded before hashing; the key itself is always 64 hex characters.
    return hashlib.sha256(f"{scope}:{identifier[:512]}".encode()).hexdigest()


_WINDOW = "to_timestamp(floor(extract(epoch FROM now()) / :w) * :w)"
_RETRY = f"ceil(extract(epoch FROM ({_WINDOW} + make_interval(secs => :w) - now())))::int"

_HIT = text(
    f"""
    INSERT INTO rate_limits (key, window_start, count) VALUES (:key, {_WINDOW}, 1)
    ON CONFLICT (key) DO UPDATE SET
        count = CASE WHEN rate_limits.window_start = EXCLUDED.window_start
                     THEN rate_limits.count + 1 ELSE 1 END,
        window_start = EXCLUDED.window_start
    RETURNING count, {_RETRY}
    """
)
_PEEK = text(
    f"""
    SELECT CASE WHEN window_start = {_WINDOW} THEN count ELSE 0 END, {_RETRY}
    FROM rate_limits WHERE key = :key
    """
)


def too_many_requests(retry_after: int) -> AppError:
    retry_after = max(retry_after, 1)
    return AppError(
        "rate_limited",
        "Too many requests. Try again later.",
        status.HTTP_429_TOO_MANY_REQUESTS,
        details={"retry_after_seconds": retry_after},
        headers={"Retry-After": str(retry_after)},
    )


def hit(db: Session, scope: str, identifier: str, limit: Limit) -> Usage:
    """Count one request and commit at once (the count must survive a failed request)."""
    count, retry = db.execute(
        _HIT, {"key": _key(scope, identifier), "w": limit.window_seconds}
    ).one()
    db.commit()
    return Usage(count, retry)


def enforce(db: Session, scope: str, identifier: str, limit: Limit) -> None:
    usage = hit(db, scope, identifier, limit)
    if usage.count > limit.max_requests:
        logger.warning("Rate limit exceeded scope=%s", scope)
        raise too_many_requests(usage.retry_after_seconds)


def ensure_below(db: Session, scope: str, identifier: str, limit: Limit) -> None:
    """Refuse when the counter is already at the limit, without counting this request
    (for limits that count only failures)."""
    row = db.execute(_PEEK, {"key": _key(scope, identifier), "w": limit.window_seconds}).first()
    if row is not None and row[0] >= limit.max_requests:
        logger.warning("Rate limit exceeded scope=%s", scope)
        raise too_many_requests(row[1])


def prune(db: Session, older_than_seconds: int = 86_400) -> int:
    """Delete counters whose window ended long ago; the caller commits."""
    result = db.execute(
        text("DELETE FROM rate_limits WHERE window_start < now() - make_interval(secs => :s)"),
        {"s": older_than_seconds},
    )
    return result.rowcount
