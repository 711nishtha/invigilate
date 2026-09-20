"""The server clock.

Exam timing is decided here and only here. Client timestamps are stored as
forensic metadata and never used for a decision. Centralising this also gives
the test suite a single place to freeze time.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def utcnow() -> datetime:
    """Timezone-aware current server time."""
    return datetime.now(timezone.utc)


def expiry_from(started_at: datetime, duration_seconds: int) -> datetime:
    return started_at + timedelta(seconds=duration_seconds)


def is_expired(expires_at: datetime, *, grace_seconds: int = 0) -> bool:
    """True when the attempt window has closed.

    ``grace_seconds`` absorbs network latency for an answer that was in flight
    at the buzzer. It is server-configured and cannot be influenced by a
    request.
    """
    return utcnow() > expires_at + timedelta(seconds=grace_seconds)


def seconds_remaining(expires_at: datetime) -> int:
    """Whole seconds left, floored at zero — never negative in a response."""
    return max(0, int((expires_at - utcnow()).total_seconds()))


def ensure_aware(value: datetime) -> datetime:
    """Treat a naive datetime as UTC.

    Rows written before a column was timezone-aware, or read back from a
    driver that drops tzinfo, must not silently compare as local time.
    """
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
