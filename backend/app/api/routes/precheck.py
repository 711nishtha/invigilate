"""Pre-exam security check routes.

Four endpoints, in the order a student moves through them, and the order is the
security model:

1. ``POST /public/quizzes/{code}/precheck`` — open a check. No attempt, no clock.
2. ``POST /public/precheck/{id}/capabilities`` — is this browser able at all?
3. ``POST /public/precheck/{id}/readiness`` — is everything actually running?
4. ``POST /public/precheck/{id}/start`` — the server creates the attempt.

Only step 4 creates an attempt, and only the server can decide that it may. The
Phase 3 endpoint ``POST /public/quizzes/{code}/attempts`` refuses any quiz that
requires a precheck, so there is no second route into a proctored exam.

Authorization on steps 2-4 is the precheck token in ``X-Precheck-Token``,
resolved token-first with a uniform 404 — the same shape as attempt tokens, for
the same reason: the endpoint must not become an existence oracle.
"""

# NOTE: no `from __future__ import annotations`. Rate-limited handlers are
# wrapped by slowapi, and FastAPI resolves annotations against the wrapper's
# module globals, where dependency aliases do not exist.
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, Response, status

from app.api.deps import DbSession
from app.core.clock import ensure_aware, utcnow
from app.core.config import settings
from app.core.errors import NotAuthenticated
from app.core.rate_limit import limiter
from app.db.models import Quiz
from app.lockdown import get_lockdown_provider
from app.schemas.proctoring import (
    CapabilityReport,
    CapabilityResponse,
    PrecheckStartExamRequest,
    PrecheckStartRequest,
    PrecheckStartResponse,
    ReadinessReport,
    ReadinessResponse,
)
from app.schemas.student import AttemptStartResponse
from app.services import attempt_service, precheck_service, proctoring_policy

router = APIRouter(prefix="/public", tags=["precheck"])


def get_precheck_token(
    x_precheck_token: Annotated[str | None, Header()] = None,
) -> str:
    """The bearer token for one precheck session. Scoped to that session only."""
    if not x_precheck_token:
        raise NotAuthenticated(
            "Missing X-Precheck-Token header.", code="MISSING_PRECHECK_TOKEN"
        )
    return x_precheck_token.strip()


PrecheckToken = Annotated[str, Depends(get_precheck_token)]


def _quiz_for(db: DbSession, session) -> Quiz:  # type: ignore[no-untyped-def]
    """The session's quiz, re-read so the *current* policy applies.

    Re-reading rather than caching is deliberate: if a teacher tightens the
    proctoring policy while a student is midway through the checks, the
    stricter policy is the one that decides.
    """
    quiz = db.get(Quiz, session.quiz_id)
    if quiz is None:  # pragma: no cover - FK makes this unreachable
        raise precheck_service.PrecheckNotReady("This check's quiz is no longer available.")
    return quiz


@router.post(
    "/quizzes/{public_code}/precheck",
    response_model=PrecheckStartResponse,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit(settings.rate_limit_precheck)
def start_precheck(
    request: Request,
    response: Response,
    public_code: str,
    payload: PrecheckStartRequest,
    db: DbSession,
) -> PrecheckStartResponse:
    """Open a pre-exam security check.

    Creates no attempt and starts no clock: a student who spends four minutes
    granting camera permissions loses none of their exam time.

    A LOCKDOWN quiz is refused here, before the student hands over any camera
    access, because no desktop lockdown client exists.
    """
    quiz = attempt_service.get_public_quiz(db, public_code)
    started = precheck_service.create_session(
        db,
        quiz,
        student_name=payload.student_name,
        student_email=payload.student_email,
    )
    capabilities = get_lockdown_provider(started.policy.mode).capabilities()
    return PrecheckStartResponse(
        precheck_id=started.session.id,
        precheck_token=started.token,
        status=started.session.status,
        mode=started.policy.mode,
        server_time=utcnow(),
        expires_at=ensure_aware(started.session.expires_at),
        requirements=started.policy.as_public_requirements(),
        # The provider's own words about what monitoring can and cannot do. The
        # student reads this before granting anything.
        monitoring_notice=list(capabilities.notes),
    )


@router.post("/precheck/{precheck_id}/capabilities", response_model=CapabilityResponse)
@limiter.limit(settings.rate_limit_precheck)
def submit_capabilities(
    request: Request,
    response: Response,
    precheck_id: str,
    payload: CapabilityReport,
    token: PrecheckToken,
    db: DbSession,
) -> CapabilityResponse:
    """Report what this browser can do, and find out whether that is enough.

    A browser that cannot meet a requirement is **blocked**. It is never given a
    quietly reduced version of a proctored exam, because the teacher would have
    no way to know it happened.
    """
    session = precheck_service.resolve_session(db, precheck_id=precheck_id, token=token)
    quiz = _quiz_for(db, session)
    session, verdict = precheck_service.submit_capabilities(
        db, session, quiz, payload.model_dump()
    )
    return CapabilityResponse(
        precheck_id=session.id,
        status=session.status,
        satisfiable=verdict.satisfiable,
        missing=list(verdict.missing),
        notes=list(verdict.notes),
    )


@router.post("/precheck/{precheck_id}/readiness", response_model=ReadinessResponse)
@limiter.limit(settings.rate_limit_precheck)
def submit_readiness(
    request: Request,
    response: Response,
    precheck_id: str,
    payload: ReadinessReport,
    token: PrecheckToken,
    db: DbSession,
) -> ReadinessResponse:
    """Submit observations. The server decides whether they add up to ready.

    The request model has no ``ready`` field and forbids extra keys, so there is
    nothing a client can send that means "let me in".
    """
    session = precheck_service.resolve_session(db, precheck_id=precheck_id, token=token)
    quiz = _quiz_for(db, session)
    session, verdict = precheck_service.submit_readiness(
        db, session, quiz, payload.model_dump()
    )
    return ReadinessResponse(
        precheck_id=session.id,
        status=session.status,
        ready=verdict.ready,
        checks=verdict.checks,
        failures=verdict.failures,
        server_time=utcnow(),
        expires_at=ensure_aware(session.expires_at),
    )


@router.post(
    "/precheck/{precheck_id}/start",
    response_model=AttemptStartResponse,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit(settings.rate_limit_attempt_start)
def start_exam(
    request: Request,
    response: Response,
    precheck_id: str,
    payload: PrecheckStartExamRequest,
    token: PrecheckToken,
    db: DbSession,
) -> AttemptStartResponse:
    """Create the attempt, if the server still authorizes it.

    The policy is re-evaluated here against the stored observations rather than
    trusting the stored ``READY``: a status is a record of a past decision, and
    this is the moment the decision takes effect. The timer starts now, from the
    server clock, inside the unmodified Phase 3 ``create_attempt``.

    The session is single-use. A second call gets ``PRECHECK_ALREADY_USED``, and
    the one-attempt-per-student rule is still enforced underneath by the unique
    constraint on ``attempts``.
    """
    # The lock matters: two tabs racing this endpoint must not both get past
    # the single-use check.
    session = precheck_service.resolve_session(
        db, precheck_id=precheck_id, token=token, for_update=True
    )
    quiz = _quiz_for(db, session)
    started = precheck_service.authorize_start(db, session, quiz)
    attempt = started.attempt
    policy = proctoring_policy.policy_for(quiz)

    return AttemptStartResponse(
        attempt_id=attempt.id,
        # Shown once. Only its hash is stored.
        attempt_token=started.token,
        server_time=utcnow(),
        started_at=ensure_aware(attempt.started_at),
        expires_at=ensure_aware(attempt.expires_at),
        seconds_remaining=attempt_service.seconds_remaining_for(attempt),
        question_count=started.question_count,
        allow_backtracking=quiz.allow_backtracking,
        security_level=quiz.security_level,
        proctoring_mode=policy.mode,
        heartbeat_interval_seconds=policy.heartbeat_interval_seconds,
    )


__all__ = ["get_precheck_token", "router"]
