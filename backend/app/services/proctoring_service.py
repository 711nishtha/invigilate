"""Proctoring during the exam: heartbeats, degradation, and review.

Phase 3 already records integrity signals and enforces a budget. This module
adds the part that only matters once monitoring is required: knowing whether
monitoring is *still working*, and saying so honestly when it is not.

The governing rule is the one the whole phase is built around:

    signal -> evidence -> policy -> teacher review

and never signal -> accusation. Nothing here produces a verdict about a person.
It produces a status about a *stream*, a timeline, and a summary that says what
was observed and how much of it the platform can actually vouch for.

Three behaviours are worth calling out because each is a decision that could
easily have gone the other way:

**Silence is recorded, not punished twice.** A browser that stops sending
heartbeats has either crashed, lost wifi, or been closed. The server cannot tell
which, so it raises one ``PROCTORING_HEARTBEAT_LOST`` per grace window and
marks the attempt ``UNVERIFIABLE``. It does not escalate on every subsequent
request, because the student is very often sitting in front of a frozen page
through no fault of their own.

**Stream loss defaults to WARN.** A teacher can escalate to FLAG or AUTO_SUBMIT,
but the default does not end an exam because a webcam driver crashed. The
academic result is the thing of value; proctoring is evidence about it.

**A proctoring failure never corrupts a result.** Nothing in this module writes
to ``score``, ``percentage`` or ``is_correct``, and nothing in it rolls back a
finalized attempt. The worst it can do is finalize an attempt early — through
the same Phase 3 finalization path, with the same grading — and flag it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.clock import ensure_aware, utcnow
from app.core.logging import Event, log_event
from app.db.models import (
    Attempt,
    IntegrityAction,
    IntegrityEvent,
    IntegrityEventType,
    ProctoringMode,
    ProctoringReview,
    ProctoringStatus,
    Quiz,
)
from app.services import integrity_policy, proctoring_policy
from app.services.proctoring_policy import ProctoringPolicy

# Observation keys a client may report in a heartbeat, mapped to the signal the
# server raises when the observation says the stream is not running.
#
# Note the direction: the client reports *state*, the server decides what the
# absence of that state means. There is no key a client can send that says
# "everything is fine, do not check".
_STREAM_CHECKS: tuple[tuple[str, str, IntegrityEventType], ...] = (
    ("require_camera", "camera_active", IntegrityEventType.CAMERA_STOPPED),
    ("require_microphone", "microphone_active", IntegrityEventType.MICROPHONE_STOPPED),
    ("require_screen_share", "screen_share_active", IntegrityEventType.SCREEN_SHARE_STOPPED),
)


@dataclass
class HeartbeatOutcome:
    """What the server concluded from one heartbeat."""

    proctoring_status: ProctoringStatus
    violation_total: int
    signals_recorded: list[str]
    auto_submitted: bool = False
    warnings: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.warnings is None:
            self.warnings = []


def _status_after(
    current: ProctoringStatus, *, impaired: bool, silent: bool
) -> ProctoringStatus:
    """Resolve the attempt's monitoring status.

    ``UNVERIFIABLE`` outranks ``DEGRADED``: not knowing is worse than knowing
    something is broken. Recovery to ``ACTIVE`` is allowed — a student who
    re-enables their camera should not carry a red badge for the rest of the
    exam — but it is driven by a fresh, positive observation, never by the mere
    passage of time.
    """
    if silent:
        return ProctoringStatus.UNVERIFIABLE
    if impaired:
        return ProctoringStatus.DEGRADED
    if current is ProctoringStatus.NOT_REQUIRED:
        return ProctoringStatus.NOT_REQUIRED
    return ProctoringStatus.ACTIVE


def check_heartbeat_staleness(
    db: Session, attempt: Attempt, quiz: Quiz
) -> IntegrityEvent | None:
    """Raise a lost-heartbeat signal if the browser has gone quiet.

    Called from the ordinary student request path, so a student who keeps
    interacting while their monitoring loop is dead is still caught. Debounced
    by the repeat window in the policy, so a long silence produces a handful of
    timeline entries rather than one per request.

    Does not commit: the caller owns the transaction.
    """
    policy = proctoring_policy.policy_for(quiz)
    if not policy.requires_precheck or attempt.is_terminal:
        return None

    last = attempt.last_heartbeat_at
    if last is None:
        return None

    silence = utcnow() - ensure_aware(last)
    if silence < timedelta(seconds=policy.heartbeat_grace_seconds):
        return None

    outcome = integrity_policy.record_event(
        db,
        attempt,
        quiz,
        event_type=IntegrityEventType.PROCTORING_HEARTBEAT_LOST,
        metadata={"silent_seconds": int(silence.total_seconds())},
        server_generated=True,
    )
    # Not knowing is its own state. It is never a pass, and it is never treated
    # as proof of misconduct either — a closed laptop and a dead wifi router
    # look identical from here.
    attempt.proctoring_status = ProctoringStatus.UNVERIFIABLE
    db.add(attempt)

    log_event(
        Event.PROCTORING_DEGRADED,
        attempt_id=str(attempt.id),
        reason="HEARTBEAT_LOST",
        silent_seconds=int(silence.total_seconds()),
        proctoring_status=attempt.proctoring_status.value,
    )
    return outcome.event


def record_heartbeat(
    db: Session,
    attempt: Attempt,
    quiz: Quiz,
    observations: dict[str, Any],
) -> HeartbeatOutcome:
    """Process one proctoring heartbeat.

    The browser reports what it currently observes. The server decides what that
    means, records any signals, updates the monitoring status and applies the
    configured stream-loss action. The browser never submits severity, a
    violation count, a suspicion score or a verdict — those fields do not exist
    on the request model, so an attempt to send one is a 422.

    Does not commit; the route owns the transaction so that a signal, the
    counter it moves and any resulting finalization are written atomically.
    """
    policy = proctoring_policy.policy_for(quiz)
    outcome = HeartbeatOutcome(
        proctoring_status=attempt.proctoring_status,
        violation_total=attempt.integrity_violation_count,
        signals_recorded=[],
    )

    attempt.last_heartbeat_at = utcnow()

    impaired = False
    threshold_exceeded = False

    # A client that admits it cannot monitor is believed and not punished. This
    # has to be free, or clients stop reporting it and the platform loses its
    # only honest signal that the green ticks are meaningless.
    if observations.get("monitoring_impaired") is True:
        impaired = True
        result = integrity_policy.record_event(
            db,
            attempt,
            quiz,
            event_type=IntegrityEventType.PROCTORING_UNAVAILABLE,
            metadata={"detail": str(observations.get("impaired_reason", ""))[:200]},
        )
        outcome.signals_recorded.append(IntegrityEventType.PROCTORING_UNAVAILABLE.value)
        threshold_exceeded = threshold_exceeded or result.threshold_exceeded

    for flag, key, event_type in _STREAM_CHECKS:
        if not getattr(policy, flag):
            continue
        if observations.get(key) is True:
            continue
        impaired = True
        result = integrity_policy.record_event(
            db, attempt, quiz, event_type=event_type, metadata={"observation": key}
        )
        outcome.signals_recorded.append(event_type.value)
        threshold_exceeded = threshold_exceeded or result.threshold_exceeded
        outcome.warnings.append(
            "A monitoring stream this exam requires is not running. "
            "Your teacher will see this on your attempt."
        )

    outcome.proctoring_status = _status_after(
        attempt.proctoring_status, impaired=impaired, silent=False
    )
    attempt.proctoring_status = outcome.proctoring_status

    if not impaired and IntegrityEventType.PROCTORING_RESUMED.value not in outcome.signals_recorded:
        # Only worth a timeline entry when it is actually a recovery.
        previously_bad = outcome.proctoring_status is ProctoringStatus.ACTIVE
        if previously_bad and _was_impaired(db, attempt):
            integrity_policy.record_event(
                db, attempt, quiz, event_type=IntegrityEventType.PROCTORING_RESUMED
            )
            outcome.signals_recorded.append(IntegrityEventType.PROCTORING_RESUMED.value)

    db.add(attempt)
    outcome.violation_total = attempt.integrity_violation_count

    if impaired:
        log_event(
            Event.PROCTORING_DEGRADED,
            attempt_id=str(attempt.id),
            reason="STREAM_LOSS",
            signals=outcome.signals_recorded,
            on_stream_loss=policy.on_stream_loss,
            proctoring_status=outcome.proctoring_status.value,
        )

    # The stream-loss action is the teacher's choice, and it is deliberately
    # separate from the integrity budget. AUTO_SUBMIT is available for exams
    # that genuinely cannot continue unmonitored; it is not the default,
    # because ending an exam over a USB hiccup punishes the wrong person.
    should_submit = threshold_exceeded or (impaired and policy.on_stream_loss == "AUTO_SUBMIT")
    outcome.auto_submitted = should_submit
    return outcome


def _was_impaired(db: Session, attempt: Attempt) -> bool:
    """Whether the most recent proctoring signal indicated a problem."""
    last = db.scalar(
        select(IntegrityEvent.event_type)
        .where(
            IntegrityEvent.attempt_id == attempt.id,
            IntegrityEvent.event_type.in_(list(proctoring_policy.DEGRADING_EVENTS)),
        )
        .order_by(IntegrityEvent.occurred_at.desc())
        .limit(1)
    )
    return last is not None


def apply_precheck_status(attempt: Attempt, policy: ProctoringPolicy) -> None:
    """Set the initial monitoring status for a newly started attempt."""
    attempt.proctoring_mode = policy.mode
    attempt.proctoring_status = (
        ProctoringStatus.ACTIVE if policy.requires_precheck else ProctoringStatus.NOT_REQUIRED
    )


# --- Teacher review -----------------------------------------------------------

# Review outcomes. Note the vocabulary: every value describes what a reviewer
# should *do*, not what a student *did*. There is no "CHEATING" bucket, because
# this platform is not in a position to fill one.
REVIEW_NO_SIGNALS = "NO_SIGNALS"
REVIEW_INFORMATIONAL = "INFORMATIONAL_ONLY"
REVIEW_RECOMMENDED = "REVIEW_RECOMMENDED"
REVIEW_MONITORING_INCOMPLETE = "MONITORING_INCOMPLETE"


@dataclass(frozen=True)
class ProctoringSummary:
    """A teacher-facing digest of one attempt's proctoring timeline."""

    mode: ProctoringMode
    proctoring_status: ProctoringStatus
    review_state: ProctoringReview
    outcome: str
    headline: str
    caveats: tuple[str, ...]
    signal_counts: dict[str, int]
    violation_total: int
    max_violations: int
    evidence_count: int
    auto_submitted: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "proctoring_status": self.proctoring_status.value,
            "review_state": self.review_state.value,
            "outcome": self.outcome,
            "headline": self.headline,
            "caveats": list(self.caveats),
            "signal_counts": dict(self.signal_counts),
            "violation_total": self.violation_total,
            "max_violations": self.max_violations,
            "evidence_count": self.evidence_count,
            "auto_submitted": self.auto_submitted,
        }


# Attached to every summary. These are not disclaimers bolted on for legal
# comfort; they are the actual epistemic limits of the data being shown, and a
# teacher making a decision needs them next to the numbers rather than in a
# document they will not open.
_UNIVERSAL_CAVEATS = (
    "Every signal below was reported by the student's browser. A browser cannot "
    "see the room, a second device, or another person outside the camera's view.",
    "These are signals, not findings. None of them establishes that a student did "
    "anything wrong.",
)


def summarize(
    db: Session, attempt: Attempt, quiz: Quiz, *, evidence_count: int = 0
) -> ProctoringSummary:
    """Build the review digest for one attempt.

    Deliberately conservative. The strongest thing it will say is that a human
    should look, and it says why. It never ranks students, never produces a
    suspicion score, and never converts a count of signals into a judgement.
    """
    counts_rows = db.execute(
        select(IntegrityEvent.event_type, func.count())
        .where(IntegrityEvent.attempt_id == attempt.id)
        .group_by(IntegrityEvent.event_type)
    ).all()
    signal_counts = {row[0].value: int(row[1]) for row in counts_rows}

    auto_submitted = (
        db.scalar(
            select(func.count())
            .select_from(IntegrityEvent)
            .where(
                IntegrityEvent.attempt_id == attempt.id,
                IntegrityEvent.action_taken == IntegrityAction.AUTO_SUBMITTED,
            )
        )
        or 0
    ) > 0

    counted = attempt.integrity_violation_count
    caveats = list(_UNIVERSAL_CAVEATS)

    if attempt.proctoring_mode is ProctoringMode.STANDARD:
        outcome = REVIEW_NO_SIGNALS if not counted else REVIEW_INFORMATIONAL
        headline = "This exam was not proctored."
        caveats.append("No camera, microphone or screen monitoring was required.")
    elif attempt.proctoring_status in (
        ProctoringStatus.UNVERIFIABLE,
        ProctoringStatus.DEGRADED,
    ):
        # The honest outcome, and the one a naive implementation would have
        # reported as a clean exam.
        outcome = REVIEW_MONITORING_INCOMPLETE
        headline = (
            "Monitoring did not run for all of this attempt, so the platform "
            "cannot describe what happened during the gaps."
        )
        caveats.append(
            "A monitoring gap is not evidence of misconduct. Equipment fails and "
            "networks drop; the platform records that it could not observe, not "
            "that something occurred."
        )
    elif counted > quiz.max_integrity_violations:
        outcome = REVIEW_RECOMMENDED
        headline = (
            "Signals on this attempt exceeded the threshold this quiz was "
            "configured with. A person should review the timeline."
        )
    elif counted > 0:
        outcome = REVIEW_INFORMATIONAL
        headline = "Some signals were recorded, below the configured threshold."
    else:
        outcome = REVIEW_NO_SIGNALS
        headline = "No signals were recorded during this attempt."

    if attempt.proctoring_mode is ProctoringMode.PROCTORED:
        caveats.append(
            "PROCTORED mode is browser-based monitoring. It does not lock the "
            "operating system and cannot prevent the use of another device."
        )

    return ProctoringSummary(
        mode=attempt.proctoring_mode,
        proctoring_status=attempt.proctoring_status,
        review_state=attempt.proctoring_review,
        outcome=outcome,
        headline=headline,
        caveats=tuple(caveats),
        signal_counts=signal_counts,
        violation_total=counted,
        max_violations=quiz.max_integrity_violations,
        evidence_count=evidence_count,
        auto_submitted=auto_submitted,
    )


def record_review(
    db: Session,
    attempt: Attempt,
    *,
    decision: ProctoringReview,
    note: str | None,
    reviewer_id: Any,
) -> Attempt:
    """Store a teacher's conclusion about the proctoring timeline.

    Attributed and timestamped, because this is a judgement about a person and
    it should be clear whose judgement it was. It changes no mark: the academic
    result and the proctoring review are separate fields, and this function
    touches only the second.
    """
    attempt.proctoring_review = decision
    attempt.proctoring_review_note = (note or "").strip()[:2000] or None
    attempt.reviewed_at = utcnow()
    attempt.reviewed_by_id = reviewer_id
    db.add(attempt)
    db.commit()
    db.refresh(attempt)

    log_event(
        Event.PROCTORING_REVIEWED,
        attempt_id=str(attempt.id),
        decision=decision.value,
        reviewer_id=str(reviewer_id),
    )
    return attempt


__all__ = [
    "REVIEW_INFORMATIONAL",
    "REVIEW_MONITORING_INCOMPLETE",
    "REVIEW_NO_SIGNALS",
    "REVIEW_RECOMMENDED",
    "HeartbeatOutcome",
    "ProctoringSummary",
    "apply_precheck_status",
    "check_heartbeat_staleness",
    "record_heartbeat",
    "record_review",
    "summarize",
]
