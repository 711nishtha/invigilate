"""Attempt finalization — the one place an attempt becomes terminal.

Three different events finish an attempt: the student submits, the integrity
policy auto-submits, or the clock runs out. All three land here, so "finalize"
means exactly one thing and the idempotency rule is written once.

Concurrency contract:

1. The caller re-reads the attempt row ``WITH FOR UPDATE`` inside the
   transaction (``resolve_attempt(..., for_update=True)``).
2. If the row is already terminal, the stored result is returned unchanged —
   no re-grade, no second email, no new timestamp.
3. Otherwise the attempt is graded, stamped and committed.

A second concurrent submit blocks on the lock in step 1, then observes the
terminal state in step 2. That is what makes ``POST /submit`` idempotent under
a real race rather than only under a sequential retry.

The lock is a PostgreSQL behaviour. SQLite serializes writes at the database
level, so these paths are *correct* there but not *tested* there; see
tests/integration/test_postgres_concurrency.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.clock import ensure_aware, utcnow
from app.core.logging import Event, log_event
from app.db.models import Attempt, AttemptStatus, IntegrityStatus


@dataclass(frozen=True)
class FinalizationResult:
    """Outcome of a finalization call.

    ``already_final`` lets the caller distinguish the request that actually
    finalized the attempt from one that arrived afterwards — which is how
    duplicate result emails are suppressed.
    """

    attempt: Attempt
    already_final: bool

    @property
    def should_send_results(self) -> bool:
        return not self.already_final and self.attempt.results_emailed_at is None


# Which status a finalization reason produces. Keyed by reason so a caller
# cannot invent a terminal state that the state machine does not allow.
_REASON_STATUS = {
    "MANUAL": AttemptStatus.SUBMITTED,
    "AUTO_INTEGRITY": AttemptStatus.AUTO_SUBMITTED,
    "TIME_EXPIRED": AttemptStatus.TIME_EXPIRED,
}

_REASON_EVENT = {
    "MANUAL": Event.MANUAL_SUBMISSION,
    "AUTO_INTEGRITY": Event.AUTO_SUBMISSION,
    "TIME_EXPIRED": Event.TIME_EXPIRED,
}


def finalize_attempt(
    db: Session,
    attempt: Attempt,
    *,
    reason: str = "MANUAL",
) -> FinalizationResult:
    """Grade and close an attempt, exactly once.

    The caller is responsible for having locked ``attempt`` — this function
    does not acquire the lock itself, because the lock has to be taken in the
    same transaction as the read that decided the attempt was still open.
    """
    if reason not in _REASON_STATUS:
        raise ValueError(f"Unknown finalization reason: {reason}")

    # The idempotency check. Everything below it runs at most once per attempt.
    if attempt.is_terminal:
        return FinalizationResult(attempt=attempt, already_final=True)

    # Imported here to keep the module import graph free of a cycle between
    # grading and finalization.
    from app.db.models import AttemptQuestion
    from app.services.grading import grade_attempt

    # Load the paper fresh, with answers eagerly attached. Grading must not use
    # ``attempt.questions``: if that collection was loaded before the answers
    # were written, each question's ``answer`` link is stale and grade_attempt
    # sees None, silently scoring a submitted answer as unanswered. A student
    # losing marks for an answer they did submit is the worst failure this
    # engine can have, so the read is explicit rather than incidental.
    # populate_existing is essential, not decorative: these rows are usually
    # already in the session's identity map with `answer` loaded as None from
    # before the student answered, and an eager loader will not overwrite an
    # already-populated attribute. Without it, grading silently sees an
    # answered question as unanswered.
    questions = list(
        db.scalars(
            select(AttemptQuestion)
            .where(AttemptQuestion.attempt_id == attempt.id)
            .options(
                selectinload(AttemptQuestion.answer),
                selectinload(AttemptQuestion.variant),
            )
            .order_by(AttemptQuestion.position)
            .execution_options(populate_existing=True)
        )
    )
    graded = grade_attempt(questions)

    attempt.score = graded.score
    attempt.max_score = graded.max_score
    attempt.percentage = graded.percentage
    attempt.status = _REASON_STATUS[reason]
    attempt.submitted_at = utcnow()

    if reason == "AUTO_INTEGRITY":
        attempt.integrity_status = IntegrityStatus.FLAGGED

    db.add(attempt)
    db.commit()
    db.refresh(attempt)

    log_event(
        _REASON_EVENT[reason],
        attempt_id=str(attempt.id),
        quiz_id=str(attempt.quiz_id),
        status=attempt.status.value,
        # The score is the teacher's data, not a secret, and having it in the
        # log is what makes a disputed mark auditable.
        score=str(attempt.score),
        max_score=str(attempt.max_score),
    )

    return FinalizationResult(attempt=attempt, already_final=False)


def finalize_if_expired(db: Session, attempt: Attempt) -> FinalizationResult | None:
    """Close an attempt whose window has passed.

    Called opportunistically on any request that touches an attempt, so a
    student who closed their laptop still ends up with a graded, finalized
    record rather than one stuck ``IN_PROGRESS`` forever.

    Note the deliberate absence of a grace period: the grace window in
    :func:`app.core.clock.is_expired` exists to accept an answer that was in
    flight at the buzzer, not to extend the exam.
    """
    if attempt.is_terminal:
        return None
    if utcnow() <= ensure_aware(attempt.expires_at):
        return None
    return finalize_attempt(db, attempt, reason="TIME_EXPIRED")


def zero_score(attempt: Attempt) -> Decimal:
    """Score for an attempt with no questions. Present so the caller never divides by zero."""
    return Decimal("0.00") if attempt.score is None else Decimal(attempt.score)


__all__ = ["FinalizationResult", "finalize_attempt", "finalize_if_expired"]
