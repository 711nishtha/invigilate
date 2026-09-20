"""Attempt authorization — the gate every student request passes through.

This is the single place that turns an ``X-Attempt-Token`` header into a
trusted ``Attempt``. Phase 4's routes call these functions instead of querying
``attempts`` themselves, so the rules below hold for every endpoint by
construction rather than by six separate reviews.

Design notes that are security-relevant:

*Lookup is by token, not by id.* The query filters on
``attempt_token_hash`` and only then checks that the row's id matches the one
the caller asked for. An attacker holding a valid token for their own attempt
who substitutes someone else's ``attempt_id`` produces a query that never
touches the victim's row, so nothing about it can leak — not even whether it
exists. The reverse order (fetch by id, then compare tokens) would turn the
endpoint into an existence oracle.

*Every failure is 404.* A wrong token, a wrong id, and a nonexistent attempt
are indistinguishable to the caller.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import ensure_aware, is_expired, seconds_remaining
from app.core.config import settings
from app.core.errors import AttemptExpired, Conflict, NotFound
from app.core.security import attempt_token_matches, hash_attempt_token
from app.db.models import Attempt


@dataclass(frozen=True)
class AttemptContext:
    """A resolved, authorized attempt plus the server's view of its clock."""

    attempt: Attempt
    seconds_remaining: int
    expired: bool

    @property
    def is_terminal(self) -> bool:
        return self.attempt.is_terminal


def _not_found() -> NotFound:
    """One message for every authorization failure.

    Distinct messages ("no such attempt" vs "wrong token") would let a caller
    tell an id that exists from one that does not.
    """
    return NotFound("No such attempt.", code="ATTEMPT_NOT_FOUND")


def resolve_attempt(
    db: Session,
    *,
    attempt_id: uuid.UUID | str,
    token: str,
    for_update: bool = False,
) -> AttemptContext:
    """Authorize a student request against exactly one attempt.

    ``for_update`` takes a row lock, which is how concurrent submissions are
    serialized (see :mod:`app.services.attempt_finalization`). It is a no-op on
    SQLite, so the tests that depend on real locking are PostgreSQL-only.
    """
    if not token:
        raise _not_found()

    statement = select(Attempt).where(Attempt.attempt_token_hash == hash_attempt_token(token))
    if for_update:
        statement = statement.with_for_update()

    attempt = db.scalar(statement)
    if attempt is None:
        raise _not_found()

    # Defence in depth: the row was already selected by token hash, so this can
    # only fail on a SHA-256 collision, but the comparison is constant-time and
    # costs nothing.
    if not attempt_token_matches(token, attempt.attempt_token_hash):
        raise _not_found()

    # The token names the attempt. A caller asking for a different id is either
    # confused or probing; either way they learn nothing.
    requested = attempt_id if isinstance(attempt_id, uuid.UUID) else _parse_uuid(attempt_id)
    if attempt.id != requested:
        raise _not_found()

    # Columns come back naive from some drivers; comparing a naive value with
    # an aware one raises, and comparing it as local time would silently give a
    # student the wrong deadline.
    expires_at = ensure_aware(attempt.expires_at)

    return AttemptContext(
        attempt=attempt,
        seconds_remaining=seconds_remaining(expires_at),
        expired=is_expired(expires_at, grace_seconds=settings.attempt_grace_seconds),
    )


def _parse_uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        # A malformed id is an authorization failure like any other.
        raise _not_found() from None


def require_active_attempt(context: AttemptContext) -> Attempt:
    """Assert the attempt may still be *written to*.

    Used by answer submission and integrity events. Reading a finished
    attempt's result is legitimate and goes through :func:`resolve_attempt`
    alone — a student is entitled to see their own submitted attempt.
    """
    if context.is_terminal:
        raise Conflict(
            "This attempt has already been submitted and cannot be changed.",
            code="ATTEMPT_ALREADY_FINALIZED",
            details={"status": context.attempt.status.value},
        )
    if context.expired:
        raise AttemptExpired("This attempt has expired.")
    return context.attempt


__all__ = ["AttemptContext", "require_active_attempt", "resolve_attempt"]
