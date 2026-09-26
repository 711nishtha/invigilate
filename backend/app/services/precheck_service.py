"""Pre-exam security checks and server-side exam start authorization.

This is the most security-sensitive module added in Phase 5, and the shape of
it is the argument.

**The problem.** A proctored exam has to establish some things before it begins:
the camera is running, the student is in frame, they are alone, they have agreed
to be monitored. All of those are observed *in the browser*, and the browser is
untrusted. The naive design has the page run its checks, decide they passed, and
call the existing start endpoint. That design has two defects, and the second is
fatal: the page decides, and the page can simply not run the checks at all.

**The design used here.** A proctored quiz cannot be started by the browser. The
only thing a browser can do is create a *precheck session*, report observations
into it, and ask the server to start the exam. The server re-evaluates the
policy at the moment of the start request and either creates the attempt or
refuses. Concretely::

    POST /public/quizzes/{code}/precheck        -> PENDING  (no attempt, no clock)
    POST /public/precheck/{id}/capabilities     -> PENDING | BLOCKED
    POST /public/precheck/{id}/readiness        -> READY | FAILED
    POST /public/precheck/{id}/start            -> CONSUMED, attempt created

and ``POST /public/quizzes/{code}/attempts`` — the Phase 3 path — **refuses**
for any quiz that is not STANDARD. That refusal is the bypass prevention: there
is no second door.

**Why the attempt is created last.** ``create_attempt`` is Phase 3 code and is
not modified. It assigns ``started_at`` from the server clock at the moment it
runs, which is exactly the behaviour wanted here — so the timer starts when the
server authorizes the exam, not when the student first opened the page. A
student who spends four minutes fighting with camera permissions loses none of
their exam time, and there is no pre-started attempt sitting in a paused state
that the finalization path would have to learn about.

**What this cannot do.** The server cannot see the room. Every observation it
evaluates is a claim by a browser, and a determined student can make a browser
claim anything. Two things make that bearable rather than pointless: claims are
paired with stored evidence a human can look at, and the claims are checked
against a policy the client never fully sees. Neither is proof, and the platform
does not present them as proof.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import ensure_aware, utcnow
from app.core.config import settings
from app.core.errors import AppError, Conflict, NotFound
from app.core.logging import Event, log_event
from app.core.security import (
    attempt_token_matches,
    generate_attempt_token,
    hash_attempt_token,
)
from app.db.models import (
    Attempt,
    IntegrityEventType,
    PrecheckSession,
    PrecheckStatus,
    ProctoringMode,
    ProctoringStatus,
    Quiz,
)
from app.lockdown import LockdownClientUnavailable, get_lockdown_provider
from app.services import attempt_service, integrity_policy, proctoring_policy
from app.services.proctoring_policy import ProctoringPolicy

# A precheck session is short-lived on purpose. A READY verdict says "the camera
# was running and a face was in frame a moment ago"; an hour later it says
# nothing at all, and a long-lived one would be a start token to pass around.
SESSION_TTL_SECONDS = 15 * 60

# Bounds on the untrusted blobs a client posts into the session.
MAX_REPORT_KEYS = 40
MAX_REPORT_VALUE_CHARS = 200


class PrecheckRequired(AppError):
    """The Phase 3 start path was used for a quiz that requires a precheck."""

    code = "PRECHECK_REQUIRED"
    status_code = 409


class PrecheckNotReady(AppError):
    code = "PRECHECK_NOT_READY"
    status_code = 409


class LockdownUnavailable(AppError):
    """LOCKDOWN was requested and there is no client that can deliver it."""

    code = "LOCKDOWN_CLIENT_REQUIRED"
    status_code = 409


@dataclass(frozen=True)
class StartedPrecheck:
    session: PrecheckSession
    token: str
    policy: ProctoringPolicy


def _not_found() -> NotFound:
    """One message for every resolution failure, as with attempt tokens."""
    return NotFound("No such security check.", code="PRECHECK_NOT_FOUND")


def sanitize_report(report: dict[str, Any] | None) -> dict[str, Any]:
    """Bound an untrusted capability/observation blob before storing it.

    Booleans survive as booleans because the policy reads them; everything else
    is stringified and truncated. The session row is not free object storage.
    """
    if not isinstance(report, dict):
        return {}
    cleaned: dict[str, Any] = {}
    for key, value in list(report.items())[:MAX_REPORT_KEYS]:
        name = str(key)[:64]
        if isinstance(value, bool) or value is None:
            cleaned[name] = value
        elif isinstance(value, (int, float)):
            cleaned[name] = value
        else:
            cleaned[name] = str(value)[:MAX_REPORT_VALUE_CHARS]
    return cleaned


# --- Creation ----------------------------------------------------------------


def create_session(
    db: Session,
    quiz: Quiz,
    *,
    student_name: str,
    student_email: str,
) -> StartedPrecheck:
    """Open a pre-exam check. Creates no attempt and starts no clock.

    LOCKDOWN is refused here rather than at start, so a student is told before
    they hand over camera access that this exam cannot run in this browser.
    """
    policy = proctoring_policy.policy_for(quiz)

    if not policy.requires_precheck:
        raise AppError(
            "This quiz does not use a pre-exam security check.",
            code="PRECHECK_NOT_APPLICABLE",
            status_code=409,
        )

    if policy.mode is ProctoringMode.LOCKDOWN:
        provider = get_lockdown_provider(policy.mode)
        try:
            provider.verify_client({})
        except LockdownClientUnavailable as exc:
            # Refusing is the point. Running this as PROCTORED would hand the
            # teacher a guarantee that does not exist.
            log_event(
                Event.PRECHECK_BLOCKED,
                quiz_id=str(quiz.id),
                mode=policy.mode.value,
                reason="LOCKDOWN_CLIENT_REQUIRED",
            )
            raise LockdownUnavailable(str(exc)) from exc

    email = (student_email or "").strip().lower()

    # Cheap, friendly pre-check. The attempts table's unique constraint is what
    # actually enforces one attempt per student; this only avoids sending a
    # student through five minutes of permission prompts for nothing.
    already = db.scalar(
        select(Attempt.id).where(Attempt.quiz_id == quiz.id, Attempt.student_email == email)
    )
    if already is not None:
        raise Conflict(
            "You have already started this quiz. Each student may attempt it once.",
            code="ATTEMPT_ALREADY_EXISTS",
        )

    token = generate_attempt_token()
    now = utcnow()
    session = PrecheckSession(
        quiz_id=quiz.id,
        student_name=(student_name or "").strip()[:200],
        student_email=email,
        session_token_hash=hash_attempt_token(token),
        status=PrecheckStatus.PENDING,
        # Copied, so a teacher editing the quiz mid-check cannot change the
        # requirements a student is already working through.
        proctoring_mode=policy.mode,
        expires_at=now + timedelta(seconds=SESSION_TTL_SECONDS),
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    log_event(
        Event.PRECHECK_STARTED,
        precheck_id=str(session.id),
        quiz_id=str(quiz.id),
        mode=policy.mode.value,
        required_capabilities=list(policy.required_capabilities),
    )
    return StartedPrecheck(session=session, token=token, policy=policy)


# --- Resolution ---------------------------------------------------------------


def resolve_session(
    db: Session, *, precheck_id: uuid.UUID | str, token: str, for_update: bool = False
) -> PrecheckSession:
    """Authorize a request against exactly one precheck session.

    Same shape as ``attempt_access.resolve_attempt``, and for the same reasons:
    the lookup is by **token hash first**, so substituting somebody else's
    session id produces a query that never touches their row. Every failure is
    the same 404, so the endpoint is not an existence oracle.
    """
    if not token:
        raise _not_found()

    statement = select(PrecheckSession).where(
        PrecheckSession.session_token_hash == hash_attempt_token(token)
    )
    if for_update:
        statement = statement.with_for_update()

    session = db.scalar(statement)
    if session is None:
        raise _not_found()

    if not attempt_token_matches(token, session.session_token_hash):
        raise _not_found()

    try:
        requested = (
            precheck_id if isinstance(precheck_id, uuid.UUID) else uuid.UUID(str(precheck_id))
        )
    except (ValueError, AttributeError, TypeError):
        raise _not_found() from None
    if session.id != requested:
        raise _not_found()

    return session


def is_expired(session: PrecheckSession) -> bool:
    return utcnow() >= ensure_aware(session.expires_at)


def _require_open(session: PrecheckSession) -> None:
    """A session may only be worked on while it is still live and unused."""
    if session.status is PrecheckStatus.CONSUMED:
        raise Conflict(
            "This security check has already been used to start an exam.",
            code="PRECHECK_ALREADY_USED",
        )
    if is_expired(session):
        session.status = PrecheckStatus.EXPIRED
        raise Conflict(
            "This security check has expired. Please start it again.",
            code="PRECHECK_EXPIRED",
        )


# --- Step 1: capabilities -----------------------------------------------------


def submit_capabilities(
    db: Session, session: PrecheckSession, quiz: Quiz, report: dict[str, Any]
) -> tuple[PrecheckSession, proctoring_policy.CapabilityVerdict]:
    """Record what the browser says it can do, and decide if that is enough.

    A browser that cannot do what the exam requires is **blocked**, not quietly
    given a reduced exam. Downgrading here would be the single most damaging
    thing this module could do: the teacher would see a PROCTORED exam in the
    results and have no way to know that this particular student's browser
    never ran a camera.
    """
    _require_open(session)
    policy = proctoring_policy.policy_for(quiz)

    cleaned = sanitize_report(report)
    session.capabilities_json = cleaned

    verdict = proctoring_policy.evaluate_capabilities(policy, cleaned)
    if verdict.satisfiable:
        session.status = PrecheckStatus.PENDING
    else:
        session.status = PrecheckStatus.BLOCKED
        log_event(
            Event.PRECHECK_BLOCKED,
            precheck_id=str(session.id),
            quiz_id=str(quiz.id),
            missing=list(verdict.missing),
        )

    session.verdict_json = {
        **(session.verdict_json or {}),
        "capabilities": {
            "satisfiable": verdict.satisfiable,
            "missing": list(verdict.missing),
            "notes": list(verdict.notes),
        },
    }
    db.add(session)
    db.commit()
    db.refresh(session)
    return session, verdict


# --- Step 2: readiness ---------------------------------------------------------


def submit_readiness(
    db: Session, session: PrecheckSession, quiz: Quiz, observations: dict[str, Any]
) -> tuple[PrecheckSession, proctoring_policy.ReadinessVerdict]:
    """Evaluate the student's observations against the policy, server-side.

    The request body carries observations only. There is no ``ready`` field to
    set, no ``checks_passed`` to fake, and a client that invents an extra field
    gets a 422 from ``StrictModel``. ``READY`` is a value only this function can
    write, and even then the start endpoint re-evaluates rather than trusting
    the stored status.
    """
    _require_open(session)

    if session.status is PrecheckStatus.BLOCKED:
        raise Conflict(
            "This browser cannot meet the requirements for this exam.",
            code="PRECHECK_BLOCKED",
            details=(session.verdict_json or {}).get("capabilities", {}),
        )

    policy = proctoring_policy.policy_for(quiz)
    cleaned = sanitize_report(observations)
    session.observations_json = cleaned

    verdict = proctoring_policy.evaluate_readiness(policy, cleaned)

    if verdict.ready:
        session.status = PrecheckStatus.READY
        session.authorized_at = utcnow()
    else:
        session.status = PrecheckStatus.FAILED
        session.authorized_at = None

    if cleaned.get("consent_accepted") is True and session.consent_accepted_at is None:
        session.consent_accepted_at = utcnow()

    session.verdict_json = {**(session.verdict_json or {}), "readiness": verdict.as_dict()}
    db.add(session)
    db.commit()
    db.refresh(session)

    log_event(
        Event.PRECHECK_EVALUATED,
        precheck_id=str(session.id),
        quiz_id=str(quiz.id),
        ready=verdict.ready,
        failed_checks=[key for key, ok in verdict.checks.items() if not ok],
    )
    return session, verdict


# --- Step 3: start -------------------------------------------------------------


def authorize_start(
    db: Session, session: PrecheckSession, quiz: Quiz
) -> attempt_service.StartedAttempt:
    """Create the attempt, if and only if the server still authorizes it.

    Everything that could have changed since readiness was submitted is checked
    again here, because a stored ``READY`` is a record of a past decision and
    this is the moment the decision takes effect:

    * the session is unused, unexpired and still READY;
    * the quiz is still PUBLISHED and still requires the same mode;
    * the policy re-evaluates to ready against the stored observations.

    The last one matters most. It means that if a teacher tightens the policy
    while a student is at the readiness screen, the stricter policy applies —
    and that a bug which somehow wrote READY without the checks passing still
    does not produce an exam.
    """
    _require_open(session)

    if session.status is not PrecheckStatus.READY:
        raise PrecheckNotReady(
            "The pre-exam security check has not passed yet.",
            details={"status": session.status.value},
        )

    policy = proctoring_policy.policy_for(quiz)

    # The mode the student was checked against must still be the mode in force.
    # Starting a LOCKDOWN exam on the strength of a PROCTORED check would be a
    # downgrade by way of a race.
    if policy.mode is not session.proctoring_mode:
        session.status = PrecheckStatus.FAILED
        db.commit()
        raise PrecheckNotReady(
            "This exam's security requirements changed. Please run the check again.",
            details={"expected": policy.mode.value},
        )

    if policy.mode is ProctoringMode.LOCKDOWN:
        try:
            get_lockdown_provider(policy.mode).verify_client(
                dict(session.capabilities_json or {})
            )
        except LockdownClientUnavailable as exc:
            raise LockdownUnavailable(str(exc)) from exc

    # Re-evaluate rather than trust the stored verdict.
    recheck = proctoring_policy.evaluate_readiness(policy, session.observations_json or {})
    if not recheck.ready:
        session.status = PrecheckStatus.FAILED
        session.verdict_json = {**(session.verdict_json or {}), "readiness": recheck.as_dict()}
        db.commit()
        raise PrecheckNotReady(
            "The pre-exam security check no longer passes.",
            details={"failures": recheck.failures},
        )

    # Phase 3 code, unmodified. It assigns started_at and expires_at from the
    # server clock right here, which is why the timer begins at authorization
    # and not when the student opened the page.
    started = attempt_service.create_attempt(
        db,
        quiz,
        student_name=session.student_name,
        student_email=session.student_email,
    )
    attempt = started.attempt

    attempt.proctoring_mode = policy.mode
    attempt.proctoring_status = ProctoringStatus.ACTIVE
    attempt.precheck_session_id = session.id
    attempt.last_heartbeat_at = utcnow()

    session.status = PrecheckStatus.CONSUMED
    session.attempt_id = attempt.id
    session.attempts_count += 1

    # A marker in the attempt's own timeline, so the audit trail of the exam
    # records that a server-side authorization preceded it.
    integrity_policy.record_event(
        db,
        attempt,
        quiz,
        event_type=IntegrityEventType.PRECHECK_PASSED,
        metadata={
            "precheck_id": str(session.id),
            "mode": policy.mode.value,
            "checks": ",".join(sorted(recheck.checks)),
        },
        server_generated=True,
    )

    db.add(attempt)
    db.add(session)
    db.commit()
    db.refresh(attempt)

    log_event(
        Event.PRECHECK_CONSUMED,
        precheck_id=str(session.id),
        attempt_id=str(attempt.id),
        quiz_id=str(quiz.id),
        mode=policy.mode.value,
    )
    return started


def require_precheck_for_direct_start(quiz: Quiz) -> None:
    """Refuse the Phase 3 start path for any quiz that needs a precheck.

    Called from ``POST /public/quizzes/{code}/attempts``. This is the whole of
    the bypass prevention: a student cannot skip the checks by calling the
    endpoint the checks were added in front of, because that endpoint declines
    to serve proctored quizzes at all.
    """
    policy = proctoring_policy.policy_for(quiz)
    if policy.requires_precheck:
        raise PrecheckRequired(
            "This exam requires a pre-exam security check before it can start.",
            details={"mode": policy.mode.value},
        )


def purge_expired(db: Session, *, limit: int = 500) -> int:
    """Mark stale sessions EXPIRED. Housekeeping, not a security control.

    Security comes from ``_require_open`` checking the clock on every request;
    this only stops the table filling with rows that say PENDING forever.
    """
    stale = list(
        db.scalars(
            select(PrecheckSession)
            .where(
                PrecheckSession.expires_at <= utcnow(),
                PrecheckSession.status.in_(
                    [
                        PrecheckStatus.PENDING,
                        PrecheckStatus.READY,
                        PrecheckStatus.FAILED,
                        PrecheckStatus.BLOCKED,
                    ]
                ),
            )
            .limit(limit)
        )
    )
    for session in stale:
        session.status = PrecheckStatus.EXPIRED
    if stale:
        db.commit()
    return len(stale)


__all__ = [
    "SESSION_TTL_SECONDS",
    "LockdownUnavailable",
    "PrecheckNotReady",
    "PrecheckRequired",
    "StartedPrecheck",
    "authorize_start",
    "create_session",
    "is_expired",
    "purge_expired",
    "require_precheck_for_direct_start",
    "resolve_session",
    "sanitize_report",
    "submit_capabilities",
    "submit_readiness",
]
