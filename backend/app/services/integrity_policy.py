"""Integrity signal policy.

What this is: a deterministic, server-side policy that records browser signals
and enforces a configured budget.

What this is not: proof of misconduct. A browser cannot see the operating
system, a second device, or the room. Every event here is a *signal* reported
by an untrusted client. The server decides what a signal is worth, what the
running total is, and what happens at the threshold — the client decides
nothing.

Severity is assigned from the table below and never read from the request. A
client-supplied violation count is discarded, because the whole point of the
count is that the client cannot influence it.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import ensure_aware, utcnow
from app.core.logging import Event, log_event
from app.services import proctoring_policy
from app.db.models import (
    Attempt,
    IntegrityAction,
    IntegrityEvent,
    IntegrityEventType,
    IntegrityStatus,
    Quiz,
)

# Cumulative severity contributed by each signal.
#
#   0  informational — recorded for the timeline, never counts as a violation
#   1  a routine departure from the exam window
#   3  a monitoring stream the student was asked to keep running has stopped
#
# Nothing here is evidence of cheating on its own. The budget exists so a
# teacher can decide how much departure from the exam window an exam tolerates.
SEVERITY: dict[IntegrityEventType, int] = {
    # --- Informational: context for the timeline ---------------------------
    IntegrityEventType.TAB_VISIBLE: 0,
    IntegrityEventType.WINDOW_FOCUS: 0,
    IntegrityEventType.FULLSCREEN_ENTER: 0,
    IntegrityEventType.NETWORK_RECONNECTED: 0,
    IntegrityEventType.SCREEN_SHARE_STARTED: 0,
    IntegrityEventType.CAMERA_STARTED: 0,
    IntegrityEventType.ATTEMPT_RESUMED: 0,
    # A dropped connection is not misconduct. Punishing it would penalise bad
    # wifi, and the server clock keeps running regardless.
    IntegrityEventType.NETWORK_DISCONNECTED: 0,
    # Right-click is noisy and weak as a signal; logged only.
    IntegrityEventType.RIGHT_CLICK: 0,
    # System-generated markers, never client-reported.
    IntegrityEventType.TIME_EXPIRED: 0,
    IntegrityEventType.AUTO_SUBMISSION_TRIGGERED: 0,
    # --- Counted toward the budget -----------------------------------------
    IntegrityEventType.TAB_HIDDEN: 1,
    IntegrityEventType.WINDOW_BLUR: 1,
    IntegrityEventType.FULLSCREEN_EXIT: 1,
    IntegrityEventType.COPY_ATTEMPT: 1,
    IntegrityEventType.CUT_ATTEMPT: 1,
    IntegrityEventType.PASTE_ATTEMPT: 1,
    IntegrityEventType.KEYBOARD_SHORTCUT_ATTEMPT: 1,
    # --- Higher weight: a monitoring stream stopped ------------------------
    IntegrityEventType.SCREEN_SHARE_STOPPED: 3,
    IntegrityEventType.CAMERA_STOPPED: 3,
    IntegrityEventType.MULTIPLE_SESSION_DETECTED: 3,
}

# Phase 5 proctoring weights live next to the policy they belong to and are
# merged in here, so there is still exactly one severity table at runtime and
# `severity_for` remains the only way to ask.
SEVERITY.update(proctoring_policy.PROCTORING_SEVERITY)

# Events the server generates itself. A client claiming one of these is
# fabricating the audit trail, so they are refused on the student endpoint.
SERVER_ONLY_EVENTS = frozenset(
    {
        IntegrityEventType.TIME_EXPIRED,
        IntegrityEventType.AUTO_SUBMISSION_TRIGGERED,
        IntegrityEventType.ATTEMPT_RESUMED,
    }
    # Proctoring markers the server raises itself; a client claiming one would
    # be writing fiction into the audit trail.
    | proctoring_policy.PROCTORING_SERVER_ONLY_EVENTS
)

# Metadata is attacker-controlled, so it is bounded before it reaches storage.
MAX_METADATA_KEYS = 20
MAX_METADATA_VALUE_CHARS = 500


def severity_for(event_type: IntegrityEventType) -> int:
    """Server-assigned weight. Unknown types are treated as informational."""
    return SEVERITY.get(event_type, 0)


def counts_toward_violations(event_type: IntegrityEventType) -> bool:
    return severity_for(event_type) > 0


def sanitize_metadata(metadata: dict | None) -> dict:
    """Bound client metadata before persisting it.

    Kept for forensics, never used for a decision, and capped so a student
    cannot use the event endpoint as free storage.
    """
    if not isinstance(metadata, dict):
        return {}
    cleaned: dict[str, str | int | float | bool | None] = {}
    for key, value in list(metadata.items())[:MAX_METADATA_KEYS]:
        name = str(key)[:64]
        if isinstance(value, (int, float, bool)) or value is None:
            cleaned[name] = value
        else:
            cleaned[name] = str(value)[:MAX_METADATA_VALUE_CHARS]
    return cleaned


@dataclass(frozen=True)
class IntegrityOutcome:
    """Result of recording one signal."""

    event: IntegrityEvent
    violation_total: int
    integrity_status: IntegrityStatus
    action_taken: IntegrityAction
    threshold_exceeded: bool


def _status_for(total: int, quiz: Quiz) -> IntegrityStatus:
    if total > quiz.max_integrity_violations:
        return IntegrityStatus.FLAGGED
    if total > 0:
        return IntegrityStatus.WARNING
    return IntegrityStatus.CLEAN


def record_event(
    db: Session,
    attempt: Attempt,
    quiz: Quiz,
    *,
    event_type: IntegrityEventType,
    client_reported_at=None,  # type: ignore[no-untyped-def]
    metadata: dict | None = None,
    server_generated: bool = False,
) -> IntegrityOutcome:
    """Record a signal and apply the configured policy.

    Does not commit or finalize: the caller owns the transaction, so the event,
    the updated counter and any resulting finalization are written together or
    not at all. Whether the threshold was crossed is returned rather than acted
    on here, which keeps this module free of submission logic.
    """
    severity = severity_for(event_type)

    # Rule: one frame is never a decision.
    #
    # Proctoring signals arrive from a loop running in a browser. A webcam that
    # blinks, a student who leans out of frame, or simply a buggy client can
    # emit the same signal many times a second. Counting each one would let an
    # honest attempt be auto-submitted by a hardware glitch, so a repeat inside
    # the signal's window is recorded for the timeline at severity zero.
    #
    # Enforced here rather than in the client, because the client is the thing
    # this protects against. Only Phase 5 signals have a window, so Phase 3
    # behaviour is unchanged.
    suppressed = False
    if severity > 0 and not server_generated:
        window = proctoring_policy.repeat_window(event_type)
        if window is not None:
            previous = db.scalar(
                select(IntegrityEvent.occurred_at)
                .where(
                    IntegrityEvent.attempt_id == attempt.id,
                    IntegrityEvent.event_type == event_type,
                    IntegrityEvent.severity > 0,
                )
                .order_by(IntegrityEvent.occurred_at.desc())
                .limit(1)
            )
            if previous is not None and utcnow() - ensure_aware(previous) < window:
                severity = 0
                suppressed = True

    total = attempt.integrity_violation_count + severity
    attempt.integrity_violation_count = total
    attempt.integrity_status = _status_for(total, quiz)

    exceeded = total > quiz.max_integrity_violations and severity > 0
    if exceeded:
        action = IntegrityAction.AUTO_SUBMITTED
    elif severity > 0:
        action = IntegrityAction.WARNING_ISSUED
    else:
        action = IntegrityAction.LOGGED

    event = IntegrityEvent(
        attempt_id=attempt.id,
        event_type=event_type,
        # Server-assigned. A severity in the request body is ignored.
        severity=severity,
        # Server receipt time is the authoritative ordering of the timeline.
        occurred_at=utcnow(),
        client_reported_at=client_reported_at,
        metadata_json=(
            {**sanitize_metadata(metadata), "repeat_suppressed": True}
            if suppressed
            else sanitize_metadata(metadata)
        ),
        action_taken=action,
    )
    db.add(event)
    db.add(attempt)

    log_event(
        Event.INTEGRITY_EVENT,
        attempt_id=str(attempt.id),
        quiz_id=str(attempt.quiz_id),
        event_type=event_type.value,
        severity=severity,
        violation_total=total,
        integrity_status=attempt.integrity_status.value,
        action_taken=action.value,
        server_generated=server_generated,
        repeat_suppressed=suppressed,
    )

    return IntegrityOutcome(
        event=event,
        violation_total=total,
        integrity_status=attempt.integrity_status,
        action_taken=action,
        threshold_exceeded=exceeded,
    )


__all__ = [
    "SERVER_ONLY_EVENTS",
    "SEVERITY",
    "IntegrityOutcome",
    "counts_toward_violations",
    "record_event",
    "sanitize_metadata",
    "severity_for",
]
