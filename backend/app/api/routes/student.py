"""Public and student exam routes.

Every handler here follows the same shape, and the order is the security
model:

1. Resolve the attempt through ``attempt_access`` (token-first lookup, 404 on
   any failure).
2. Finalize it if the server clock says it has expired.
3. Only then act.

Step 2 is what makes the timer authoritative rather than advisory: a student
who leaves the tab open past the deadline has their attempt closed by the next
request that touches it, whatever that request was trying to do.
"""

# NOTE: no `from __future__ import annotations` here. Rate-limited handlers are
# wrapped by slowapi, and FastAPI then resolves annotations against the
# wrapper's module globals, where our dependency aliases do not exist.
from fastapi import APIRouter, File, Form, Request, Response, UploadFile, status

from app.api.deps import AttemptToken, DbSession
from app.core.clock import ensure_aware, utcnow
from app.core.config import settings
from app.core.errors import AppError, Conflict
from app.core.rate_limit import limiter
from app.db.models import Attempt, EvidenceKind, IntegrityEventType, Quiz
from app.schemas.proctoring import (
    EvidenceUploadResponse,
    ProctoringHeartbeatRequest,
    ProctoringHeartbeatResponse,
)
from app.schemas.student import (
    AnswerRequest,
    AnswerResponse,
    AttemptQuestionsOut,
    AttemptResultOut,
    AttemptStartRequest,
    AttemptStartResponse,
    AttemptStateOut,
    IntegrityEventRequest,
    IntegrityEventResponse,
    PublicQuizOut,
    StudentQuestionOut,
    SubmitRequest,
)
from app.services import (
    attempt_service,
    evidence_service,
    integrity_policy,
    precheck_service,
    proctoring_policy,
    proctoring_service,
)
from app.services.attempt_access import require_active_attempt, resolve_attempt
from app.services.attempt_finalization import finalize_attempt, finalize_if_expired

public_router = APIRouter(prefix="/public", tags=["public"])
student_router = APIRouter(prefix="/student", tags=["student"])


def _quiz_of(db: DbSession, attempt: Attempt) -> Quiz:
    quiz = db.get(Quiz, attempt.quiz_id)
    if quiz is None:  # pragma: no cover - FK makes this unreachable
        raise AppError("This attempt's quiz is missing.", code="QUIZ_MISSING")
    return quiz


def _load(db: DbSession, attempt_id: str, token: str, *, for_update: bool = False):  # type: ignore[no-untyped-def]
    """Resolve, then close the attempt if the clock has run out.

    Returns the attempt and its quiz. Callers that intend to write then use
    ``require_active_attempt`` to refuse a terminal or expired attempt.
    """
    context = resolve_attempt(db, attempt_id=attempt_id, token=token, for_update=for_update)
    quiz = _quiz_of(db, context.attempt)

    if context.expired and not context.attempt.is_terminal:
        # The deadline passed while nobody was looking. Close and grade it now
        # rather than leaving an attempt stuck IN_PROGRESS forever.
        finalize_if_expired(db, context.attempt)
        context = resolve_attempt(db, attempt_id=attempt_id, token=token, for_update=for_update)
    elif not context.attempt.is_terminal:
        # A student can keep answering questions while their monitoring loop is
        # dead, so silence is checked on the ordinary request path rather than
        # only on the heartbeat endpoint — which a client could simply stop
        # calling. Debounced by the repeat window, and never punitive: it marks
        # the attempt UNVERIFIABLE, which is a statement about what the server
        # can see, not about the student.
        if proctoring_service.check_heartbeat_staleness(db, context.attempt, quiz):
            db.commit()

    return context, quiz


def _state(attempt: Attempt, quiz: Quiz, questions) -> AttemptStateOut:  # type: ignore[no-untyped-def]
    return AttemptStateOut(
        attempt_id=attempt.id,
        status=attempt.status,
        server_time=utcnow(),
        started_at=ensure_aware(attempt.started_at),
        expires_at=ensure_aware(attempt.expires_at),
        seconds_remaining=attempt_service.seconds_remaining_for(attempt),
        question_count=len(questions),
        answered_count=attempt_service.answered_count(questions),
        allow_backtracking=quiz.allow_backtracking,
        security_level=quiz.security_level,
        proctoring_mode=attempt.proctoring_mode,
        proctoring_status=attempt.proctoring_status,
        heartbeat_interval_seconds=(
            proctoring_policy.policy_for(quiz).heartbeat_interval_seconds
            if attempt.proctoring_mode.value != "STANDARD"
            else 0
        ),
        integrity_status=attempt.integrity_status,
        violation_count=attempt.integrity_violation_count,
        max_integrity_violations=quiz.max_integrity_violations,
        is_terminal=attempt.is_terminal,
    )


def _result(db: DbSession, attempt: Attempt, quiz: Quiz) -> AttemptResultOut:
    questions = attempt_service.load_paper(db, attempt)
    # The server decides whether a score may be seen. When it may not, the
    # numbers are absent from the payload rather than hidden by the client.
    visible = bool(quiz.show_score_after_submission) and attempt.is_terminal
    return AttemptResultOut(
        attempt_id=attempt.id,
        status=attempt.status,
        submitted_at=ensure_aware(attempt.submitted_at) if attempt.submitted_at else None,
        server_time=utcnow(),
        question_count=len(questions),
        answered_count=attempt_service.answered_count(questions),
        integrity_status=attempt.integrity_status,
        violation_count=attempt.integrity_violation_count,
        score_visible=visible,
        score=float(attempt.score) if visible and attempt.score is not None else None,
        max_score=float(attempt.max_score) if visible and attempt.max_score is not None else None,
        percentage=(
            float(attempt.percentage) if visible and attempt.percentage is not None else None
        ),
    )


# --- Public ----------------------------------------------------------------


@public_router.get("/quizzes/{public_code}", response_model=PublicQuizOut)
def get_public_quiz(public_code: str, db: DbSession) -> PublicQuizOut:
    """Pre-exam summary for /q/{code}. No auth, and no question content."""
    quiz = attempt_service.get_public_quiz(db, public_code)
    return PublicQuizOut(
        public_code=quiz.public_code or "",
        title=quiz.title,
        description=quiz.description,
        duration_seconds=quiz.duration_seconds,
        question_count=quiz.question_count,
        security_level=quiz.security_level,
        max_integrity_violations=quiz.max_integrity_violations,
        allow_backtracking=quiz.allow_backtracking,
        requires_fullscreen=quiz.security_level.value in ("SECURE", "STRICT"),
        shows_score_after_submission=quiz.show_score_after_submission,
        owner_name=quiz.owner.name if quiz.owner else None,
    )


@public_router.post(
    "/quizzes/{public_code}/attempts",
    response_model=AttemptStartResponse,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit(settings.rate_limit_attempt_start)
def start_attempt(
    request: Request,
    response: Response,  # slowapi injects rate-limit headers into this
    public_code: str,
    payload: AttemptStartRequest,
    db: DbSession,
) -> AttemptStartResponse:
    """Create the attempt and assemble its paper.

    Rate limited: this is the only unauthenticated write in the API, and it
    creates rows.
    """
    quiz = attempt_service.get_public_quiz(db, public_code)

    # Bypass prevention, and the whole of it. A proctored exam cannot be
    # started through this endpoint at all, so a student cannot skip the
    # pre-exam checks by calling the route the checks were put in front of.
    # There is no flag, no header and no payload field that relaxes this; the
    # only way into a proctored exam is POST /public/precheck/{id}/start, and
    # that route re-evaluates the policy server-side before it creates
    # anything.
    precheck_service.require_precheck_for_direct_start(quiz)

    started = attempt_service.create_attempt(
        db,
        quiz,
        student_name=payload.student_name,
        student_email=str(payload.student_email),
    )
    attempt = started.attempt
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
        proctoring_mode=attempt.proctoring_mode,
        heartbeat_interval_seconds=0,
    )


# --- Student (attempt token) -----------------------------------------------


@student_router.get("/attempts/{attempt_id}", response_model=AttemptStateOut)
def get_attempt(attempt_id: str, token: AttemptToken, db: DbSession) -> AttemptStateOut:
    """Attempt state and the authoritative clock.

    The exam runner polls this and resynchronizes its countdown against
    ``server_time`` / ``seconds_remaining``. The browser's own clock is never
    consulted.
    """
    context, quiz = _load(db, attempt_id, token)
    questions = attempt_service.load_paper(db, context.attempt)
    return _state(context.attempt, quiz, questions)


@student_router.get("/attempts/{attempt_id}/questions", response_model=AttemptQuestionsOut)
def get_questions(attempt_id: str, token: AttemptToken, db: DbSession) -> AttemptQuestionsOut:
    """The frozen paper, sanitized.

    Read from ``attempt_questions``; never re-planned. A refresh returns the
    identical paper with options in the identical order.
    """
    context, _quiz = _load(db, attempt_id, token)
    questions = attempt_service.load_paper(db, context.attempt)

    return AttemptQuestionsOut(
        attempt_id=context.attempt.id,
        server_time=utcnow(),
        seconds_remaining=attempt_service.seconds_remaining_for(context.attempt),
        questions=[
            StudentQuestionOut(
                attempt_question_id=question.id,
                position=question.position,
                question_type=question.variant.family.question_type,
                question_text=question.variant.question_text,
                options=attempt_service.displayed_options(question),
                selected_answer=question.answer.selected_answer if question.answer else None,
            )
            for question in questions
        ],
    )


@student_router.post("/attempts/{attempt_id}/answers", response_model=AnswerResponse)
@limiter.limit(settings.rate_limit_answer)
def save_answer(
    request: Request,
    response: Response,
    attempt_id: str,
    payload: AnswerRequest,
    token: AttemptToken,
    db: DbSession,
) -> AnswerResponse:
    """Save one answer. Refused once the attempt is terminal or expired."""
    context, _quiz = _load(db, attempt_id, token)
    attempt = require_active_attempt(context)

    answer = attempt_service.save_answer(
        db, attempt, payload.attempt_question_id, payload.selected_answer
    )
    return AnswerResponse(
        attempt_question_id=payload.attempt_question_id,
        answered_at=ensure_aware(answer.answered_at),
        server_time=utcnow(),
        seconds_remaining=attempt_service.seconds_remaining_for(attempt),
    )


@student_router.post(
    "/attempts/{attempt_id}/integrity-events", response_model=IntegrityEventResponse
)
@limiter.limit(settings.rate_limit_event)
def record_integrity_event(
    request: Request,
    response: Response,
    attempt_id: str,
    payload: IntegrityEventRequest,
    token: AttemptToken,
    db: DbSession,
) -> IntegrityEventResponse:
    """Record a browser integrity signal and apply the configured policy.

    If the signal takes the attempt over its budget, the **server** finalizes
    it inside this request. The browser is told what happened afterwards; it is
    never asked to perform the submission, so killing JavaScript at the moment
    of the decisive event does not avoid it.
    """
    if payload.event_type in integrity_policy.SERVER_ONLY_EVENTS:
        raise AppError(
            "That event type is recorded by the server, not the client.",
            code="SERVER_ONLY_EVENT",
            details={"event_type": payload.event_type.value},
        )

    # The lock is taken here because this request may finalize the attempt, and
    # it must not race a concurrent submit.
    context, quiz = _load(db, attempt_id, token, for_update=True)
    attempt = require_active_attempt(context)

    outcome = integrity_policy.record_event(
        db,
        attempt,
        quiz,
        event_type=payload.event_type,
        client_reported_at=payload.client_reported_at,
        metadata=payload.metadata,
    )

    auto_submitted = False
    if outcome.threshold_exceeded:
        # Marker in the timeline, then finalize — both inside this request and
        # this transaction, so the browser cannot be the thing that submits.
        integrity_policy.record_event(
            db,
            attempt,
            quiz,
            event_type=IntegrityEventType.AUTO_SUBMISSION_TRIGGERED,
            server_generated=True,
        )
        finalize_attempt(db, attempt, reason="AUTO_INTEGRITY")
        auto_submitted = True
    else:
        db.commit()

    db.refresh(attempt)
    return IntegrityEventResponse(
        recorded=True,
        violation_total=attempt.integrity_violation_count,
        max_integrity_violations=quiz.max_integrity_violations,
        integrity_status=attempt.integrity_status,
        action_taken=outcome.action_taken.value,
        auto_submitted=auto_submitted,
        attempt_status=attempt.status,
    )


@student_router.post(
    "/attempts/{attempt_id}/proctoring/heartbeat",
    response_model=ProctoringHeartbeatResponse,
)
@limiter.limit(settings.rate_limit_heartbeat)
def proctoring_heartbeat(
    request: Request,
    response: Response,
    attempt_id: str,
    payload: ProctoringHeartbeatRequest,
    token: AttemptToken,
    db: DbSession,
) -> ProctoringHeartbeatResponse:
    """Report observed monitoring state.

    The browser says what it currently sees — the camera track is live, the
    screen share is running. The server decides what that means, records any
    signals at server-assigned severity, updates the monitoring status and
    applies the configured stream-loss action.

    There is no field on the request that carries severity, a violation count, a
    suspicion score or a verdict, and ``StrictModel`` rejects unknown keys, so an
    attempt to supply one is a 422 rather than something quietly ignored.

    Missing heartbeats are not caught here — a client can simply stop calling
    this endpoint. They are caught in ``_load``, on every other student request.
    """
    context, quiz = _load(db, attempt_id, token, for_update=True)
    attempt = require_active_attempt(context)

    outcome = proctoring_service.record_heartbeat(
        db, attempt, quiz, payload.model_dump(exclude={"client_reported_at"})
    )

    if outcome.auto_submitted:
        integrity_policy.record_event(
            db,
            attempt,
            quiz,
            event_type=IntegrityEventType.AUTO_SUBMISSION_TRIGGERED,
            server_generated=True,
        )
        # The same Phase 3 finalization path, with the same grading. A
        # proctoring decision can end an exam early; it cannot change a mark.
        finalize_attempt(db, attempt, reason="AUTO_PROCTORING")
    else:
        db.commit()

    db.refresh(attempt)
    policy = proctoring_policy.policy_for(quiz)
    return ProctoringHeartbeatResponse(
        acknowledged=True,
        server_time=utcnow(),
        proctoring_status=attempt.proctoring_status,
        integrity_status=attempt.integrity_status,
        violation_total=attempt.integrity_violation_count,
        max_integrity_violations=quiz.max_integrity_violations,
        next_heartbeat_seconds=policy.heartbeat_interval_seconds,
        signals_recorded=outcome.signals_recorded,
        warnings=outcome.warnings,
        auto_submitted=outcome.auto_submitted,
        attempt_status=attempt.status.value,
    )


@student_router.post(
    "/attempts/{attempt_id}/proctoring/evidence",
    response_model=EvidenceUploadResponse,
)
@limiter.limit(settings.rate_limit_evidence)
async def upload_evidence(
    request: Request,
    response: Response,
    attempt_id: str,
    token: AttemptToken,
    db: DbSession,
    kind: str = Form(...),
    file: UploadFile = File(...),
) -> EvidenceUploadResponse:
    """Store one evidence artefact for this attempt.

    This is what makes a proctoring signal reviewable: "no face detected" is an
    assertion by an untrusted browser, but a still image at that moment is
    something a human can look at and disagree with.

    Write-only from the student's side. The response carries no URL and no path:
    a student uploads evidence about themselves and does not get to read the
    store back. Teachers read it through a signed URL minted after an ownership
    check.

    Bounded hard — size, content type and kind are all validated before the
    bytes are stored — because this endpoint is authenticated by nothing but an
    attempt token and must not double as free object storage. A storage failure
    returns ``stored=false`` rather than raising: losing a picture must never
    cost an exam.
    """
    context, quiz = _load(db, attempt_id, token)
    attempt = require_active_attempt(context)
    policy = proctoring_policy.policy_for(quiz)

    try:
        evidence_kind = EvidenceKind(kind)
    except ValueError:
        raise AppError(
            "Unknown evidence kind.",
            code="EVIDENCE_KIND_INVALID",
            details={"allowed": [k.value for k in EvidenceKind]},
        ) from None

    # Read at most one byte past the limit, so an oversized upload is refused
    # without buffering it.
    limit = evidence_service.max_bytes_for(evidence_kind)
    content = await file.read(limit + 1)
    try:
        evidence_service.validate_artefact(
            evidence_kind, file.content_type or "", len(content)
        )
    except evidence_service.EvidenceRejected as exc:
        raise AppError(str(exc), code="EVIDENCE_REJECTED") from exc

    captured = await evidence_service.capture(
        db,
        attempt,
        policy,
        kind=evidence_kind,
        content=content,
        content_type=file.content_type or "application/octet-stream",
    )
    db.commit()

    if captured.evidence is None:
        return EvidenceUploadResponse(stored=False, reason=captured.reason)
    return EvidenceUploadResponse(
        stored=True,
        evidence_id=captured.evidence.id,
        kind=captured.evidence.kind,
        retention_expires_at=ensure_aware(captured.evidence.retention_expires_at),
    )


@student_router.post("/attempts/{attempt_id}/submit", response_model=AttemptResultOut)
def submit_attempt(
    attempt_id: str,
    payload: SubmitRequest,
    token: AttemptToken,
    db: DbSession,
) -> AttemptResultOut:
    """Finalize the attempt. Idempotent under a row lock.

    A second submit blocks on the lock, sees the terminal state and returns the
    same stored result — one finalization, one score.
    """
    context, quiz = _load(db, attempt_id, token, for_update=True)

    if context.attempt.is_terminal:
        return _result(db, context.attempt, quiz)

    if context.expired:
        # Reached only if expiry happened between the load and here.
        finalize_if_expired(db, context.attempt)
    else:
        finalize_attempt(db, context.attempt, reason="MANUAL")

    db.refresh(context.attempt)
    return _result(db, context.attempt, quiz)


@student_router.get("/attempts/{attempt_id}/result", response_model=AttemptResultOut)
def get_result(attempt_id: str, token: AttemptToken, db: DbSession) -> AttemptResultOut:
    """The student's own result.

    A token may read its own finished attempt — a student is entitled to see
    their result page. It may not write to it.
    """
    context, quiz = _load(db, attempt_id, token)
    if not context.attempt.is_terminal:
        raise Conflict(
            "This attempt has not been submitted yet.", code="ATTEMPT_NOT_FINALIZED"
        )
    return _result(db, context.attempt, quiz)


__all__ = ["public_router", "student_router"]
