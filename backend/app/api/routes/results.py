"""Teacher results, export and delivery routes.

Every handler resolves its quiz or attempt through ``services.ownership``, so
the ownership predicate is inside the query and a resource the caller does not
own returns 404 rather than 403.
"""

# NOTE: no `from __future__ import annotations` here. Rate-limited handlers are
# wrapped by slowapi, and FastAPI then resolves annotations against the
# wrapper's module globals, where our dependency aliases do not exist.

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.api.deps import CurrentTeacher, DbSession
from app.core.clock import ensure_aware
from app.core.errors import AppError, NotFound
from app.core.config import settings
from app.core.rate_limit import limiter
from app.db.models import Attempt, Quiz, ResultDelivery
from app.schemas.results import (
    AttemptDetailOut,
    AttemptQuestionDetailOut,
    AttemptRowOut,
    DeliveryOut,
    IntegrityEventOut,
    QuizResultsOut,
    QuizStatsOut,
    ScoreBand,
    SendResultsRequest,
    SendResultsResponse,
)
from app.services import delivery_service, export_service, results_service
from app.services.ownership import get_owned_quiz, parse_uuid

router = APIRouter(tags=["results"])


def _stats_out(stats: results_service.QuizResultStats) -> QuizStatsOut:
    return QuizStatsOut(
        attempt_count=stats.attempt_count,
        in_progress_count=stats.in_progress_count,
        submitted_count=stats.submitted_count,
        auto_submitted_count=stats.auto_submitted_count,
        expired_count=stats.expired_count,
        finalized_count=stats.finalized_count,
        completion_rate=stats.completion_rate,
        average_percentage=stats.average_percentage,
        highest_percentage=stats.highest_percentage,
        lowest_percentage=stats.lowest_percentage,
        average_score=stats.average_score,
        max_score=stats.max_score,
        clean_count=stats.clean_count,
        warning_count=stats.warning_count,
        flagged_count=stats.flagged_count,
        integrity_event_count=stats.integrity_event_count,
        score_distribution=[
            ScoreBand(band=str(band["band"]), count=int(band["count"]))
            for band in stats.score_distribution
        ],
    )


def _delivery_out(delivery: ResultDelivery) -> DeliveryOut:
    return DeliveryOut(
        id=delivery.id,
        status=delivery.status,
        recipient=delivery.recipient,
        provider=delivery.provider,
        provider_message_id=delivery.provider_message_id,
        attempt_count=delivery.attempt_count,
        attempts_made=delivery.attempts_made,
        error_code=delivery.error_code,
        error_detail=delivery.error_detail,
        retained=bool(delivery.export_object_path),
        created_at=ensure_aware(delivery.created_at),
        sent_at=ensure_aware(delivery.sent_at) if delivery.sent_at else None,
        failed_at=ensure_aware(delivery.failed_at) if delivery.failed_at else None,
    )


def _owned_attempt(db: DbSession, attempt_id: str, teacher) -> Attempt:  # type: ignore[no-untyped-def]
    """Load an attempt whose quiz the caller owns, or 404.

    The join to ``quizzes`` is what enforces ownership: another teacher's
    attempt simply does not match, so there is no row to forget to check.
    """
    attempt = db.scalar(
        select(Attempt)
        .join(Quiz, Attempt.quiz_id == Quiz.id)
        .where(
            Attempt.id == parse_uuid(attempt_id, what="attempt"),
            Quiz.owner_id == teacher.id,
        )
    )
    if attempt is None:
        raise NotFound("No such attempt.", code="ATTEMPT_NOT_FOUND")
    return attempt


# --- Results ---------------------------------------------------------------


@router.get("/quizzes/{quiz_id}/results", response_model=QuizResultsOut)
def get_quiz_results(quiz_id: str, teacher: CurrentTeacher, db: DbSession) -> QuizResultsOut:
    """Statistics and the attempt table for one quiz.

    With no attempts every counter is zero and every average is null, so the UI
    can render a real empty state instead of a fabricated one.
    """
    quiz = get_owned_quiz(db, quiz_id, teacher)
    stats = results_service.quiz_result_stats(db, quiz)
    rows = results_service.attempt_rows(db, quiz)

    return QuizResultsOut(
        quiz_id=quiz.id,
        title=quiz.title,
        public_code=quiz.public_code,
        question_count=quiz.question_count,
        stats=_stats_out(stats),
        attempts=[
            AttemptRowOut(
                attempt_id=row.attempt_id,
                student_name=row.student_name,
                student_email=row.student_email,
                status=row.status,
                started_at=row.started_at,  # type: ignore[arg-type]
                submitted_at=row.submitted_at,  # type: ignore[arg-type]
                duration_seconds=row.duration_seconds,
                score=row.score,
                max_score=row.max_score,
                percentage=row.percentage,
                integrity_status=row.integrity_status,
                violation_count=row.violation_count,
                answered_count=row.answered_count,
                question_count=row.question_count,
            )
            for row in rows
        ],
    )


@router.get("/attempts/{attempt_id}/detail", response_model=AttemptDetailOut)
def get_attempt_detail(
    attempt_id: str, teacher: CurrentTeacher, db: DbSession
) -> AttemptDetailOut:
    """One attempt: the exact paper served, answers, marks and timeline."""
    owned = _owned_attempt(db, attempt_id, teacher)
    attempt = results_service.load_attempt_detail(db, owned.id)
    if attempt is None:  # pragma: no cover - just loaded above
        raise NotFound("No such attempt.", code="ATTEMPT_NOT_FOUND")

    quiz = db.get(Quiz, attempt.quiz_id)
    duration = None
    if attempt.submitted_at is not None:
        duration = int(
            (
                ensure_aware(attempt.submitted_at) - ensure_aware(attempt.started_at)
            ).total_seconds()
        )

    questions = []
    for question in sorted(attempt.questions, key=lambda item: item.position):
        variant = question.variant
        options = None
        if variant.options_json:
            order = question.options_order_json or list(range(len(variant.options_json)))
            options = [variant.options_json[index] for index in order]
        questions.append(
            AttemptQuestionDetailOut(
                position=question.position,
                question_type=variant.family.question_type,
                topic=variant.family.topic,
                difficulty=variant.family.difficulty,
                original_question=variant.family.original_question,
                question_text=variant.question_text,
                options=options,
                selected_answer=results_service.selected_answer_text(question),
                correct_answer=results_service.canonical_answer_text(question),
                is_correct=question.answer.is_correct if question.answer else None,
                answered_at=(
                    ensure_aware(question.answer.answered_at) if question.answer else None
                ),
            )
        )

    return AttemptDetailOut(
        attempt_id=attempt.id,
        quiz_id=attempt.quiz_id,
        quiz_title=quiz.title if quiz else "",
        student_name=attempt.student_name,
        student_email=attempt.student_email,
        status=attempt.status,
        started_at=ensure_aware(attempt.started_at),
        submitted_at=ensure_aware(attempt.submitted_at) if attempt.submitted_at else None,
        duration_seconds=duration,
        score=float(attempt.score) if attempt.score is not None else None,
        max_score=float(attempt.max_score) if attempt.max_score is not None else None,
        percentage=float(attempt.percentage) if attempt.percentage is not None else None,
        integrity_status=attempt.integrity_status,
        violation_count=attempt.integrity_violation_count,
        questions=questions,
        integrity_events=[
            IntegrityEventOut(
                event_type=event.event_type,
                severity=event.severity,
                occurred_at=ensure_aware(event.occurred_at),
                client_reported_at=(
                    ensure_aware(event.client_reported_at) if event.client_reported_at else None
                ),
                action_taken=event.action_taken.value,
                metadata=event.metadata_json or {},
            )
            for event in sorted(attempt.integrity_events, key=lambda item: item.occurred_at)
        ],
    )


# --- Export ----------------------------------------------------------------


@router.post("/quizzes/{quiz_id}/export")
@limiter.limit(settings.rate_limit_export)
def export_results(
    request: Request,
    response: Response,  # slowapi injects rate-limit headers into this
    quiz_id: str,
    teacher: CurrentTeacher,
    db: DbSession,
) -> StreamingResponse:
    """Generate and stream the results workbook.

    Built server-side from database truth and streamed straight to the teacher.
    Nothing touches the container filesystem, which Render destroys on deploy.
    """
    quiz = get_owned_quiz(db, quiz_id, teacher)
    payload = export_service.build_workbook(db, quiz)
    filename = export_service.export_filename(quiz)

    return StreamingResponse(
        iter([payload]),
        media_type=export_service.XLSX_CONTENT_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(payload)),
        },
    )


# --- Delivery --------------------------------------------------------------


@router.get("/quizzes/{quiz_id}/deliveries", response_model=list[DeliveryOut])
def list_deliveries(quiz_id: str, teacher: CurrentTeacher, db: DbSession) -> list[DeliveryOut]:
    """Delivery history, so a teacher can see a failure and retry it."""
    quiz = get_owned_quiz(db, quiz_id, teacher)
    return [_delivery_out(row) for row in delivery_service.recent_deliveries(db, quiz)]


@router.post("/quizzes/{quiz_id}/email-results", response_model=SendResultsResponse)
@limiter.limit(settings.rate_limit_email)
async def email_results(
    request: Request,
    response: Response,
    quiz_id: str,
    payload: SendResultsRequest,
    teacher: CurrentTeacher,
    db: DbSession,
) -> SendResultsResponse:
    """Email the results workbook to the quiz owner.

    Returns **200 even when delivery fails**: the results are already durable,
    so a provider outage is a reportable event rather than a server error. The
    delivery row records the failure and the teacher can retry.

    Rate limited to prevent an accidental email storm from a double-click or an
    impatient retry loop.
    """
    quiz = get_owned_quiz(db, quiz_id, teacher)
    if quiz.owner is None or not quiz.owner.email:  # pragma: no cover - FK guarantees an owner
        raise AppError("This quiz has no owner email address.", code="OWNER_EMAIL_MISSING")

    outcome = await delivery_service.send_results_email(db, quiz, requested_by=teacher)
    return SendResultsResponse(
        sent=outcome.sent,
        delivery=_delivery_out(outcome.delivery),
        error_code=outcome.error_code,
        error_message=outcome.error_message,
    )


@router.get("/deliveries/{delivery_id}/export")
async def download_delivery_export(
    delivery_id: str, teacher: CurrentTeacher, db: DbSession
) -> dict[str, str]:
    """A short-lived signed URL for a retained export.

    Authorization happens here, server-side, before any URL is minted: the
    delivery must belong to a quiz the caller owns. The bucket itself is
    private, so the signed URL is the only way in and it expires.
    """
    delivery = db.scalar(
        select(ResultDelivery)
        .join(Quiz, ResultDelivery.quiz_id == Quiz.id)
        .where(
            ResultDelivery.id == parse_uuid(delivery_id, what="delivery"),
            Quiz.owner_id == teacher.id,
        )
    )
    if delivery is None:
        raise NotFound("No such delivery.", code="DELIVERY_NOT_FOUND")
    if not delivery.export_object_path:
        raise NotFound(
            "No export was retained for this delivery.", code="EXPORT_NOT_RETAINED"
        )

    from app.storage.base import StorageError
    from app.storage.factory import get_storage_provider

    provider = get_storage_provider()
    try:
        url = await provider.signed_url(
            bucket=settings.supabase_storage_bucket_exports,
            path=delivery.export_object_path,
            expires_in=settings.export_signed_url_ttl_seconds,
        )
    except StorageError as exc:
        raise AppError(
            "The stored export is temporarily unavailable. You can regenerate it instead.",
            code="EXPORT_UNAVAILABLE",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            details={"reason": type(exc).__name__},
        ) from exc
    finally:
        await provider.aclose()

    return {"url": url, "expires_in_seconds": str(settings.export_signed_url_ttl_seconds)}


__all__ = ["router"]
