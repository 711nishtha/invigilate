"""Result delivery: export, optional storage, email, retry.

The rule this module exists to enforce:

    quiz completed -> results stored -> email attempted -> email fails
    -> results remain stored -> teacher retries

Nothing here writes to ``attempts``, ``answers`` or ``integrity_events``. A
delivery is recorded in its own table, so a Resend outage, a Storage outage, or
both together cannot roll back or alter a single mark. The workbook is always
regenerable from the database, which is why a failed upload is a logged
inconvenience rather than data loss.
"""

from __future__ import annotations

import html
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.config import settings
from app.core.logging import Event, get_logger, log_event
from app.db.models import DeliveryKind, DeliveryStatus, Quiz, ResultDelivery, User
from app.email.base import EmailAttachment, EmailMessage, EmailProviderError
from app.email.factory import get_email_provider
from app.services import export_service, results_service
from app.storage.base import StorageError
from app.storage.factory import get_storage_provider

logger = get_logger("securequiz.delivery")


@dataclass(frozen=True)
class DeliveryOutcome:
    delivery: ResultDelivery
    sent: bool
    error_code: str | None = None
    error_message: str | None = None


def _export_object_path(quiz: Quiz, delivery_id: str) -> str:
    """Namespaced by quiz so one quiz's exports cannot collide with another's."""
    return f"quizzes/{quiz.id}/results/{delivery_id}.xlsx"


async def _store_export(quiz: Quiz, delivery: ResultDelivery, payload: bytes) -> str | None:
    """Persist the workbook if retention is configured.

    Returns the object path, or None when retention is off or the upload
    failed. A failure here is deliberately non-fatal: the teacher still gets
    the email with the workbook attached, and the export can be rebuilt.
    """
    if not settings.retain_exports:
        return None

    provider = get_storage_provider()
    path = _export_object_path(quiz, str(delivery.id))
    try:
        await provider.upload(
            bucket=settings.supabase_storage_bucket_exports,
            path=path,
            content=payload,
            content_type=export_service.XLSX_CONTENT_TYPE,
        )
        return path
    except StorageError as exc:
        logger.warning(
            "Export upload failed; continuing without retention",
            extra={
                "quiz_id": str(quiz.id),
                "delivery_id": str(delivery.id),
                "error": type(exc).__name__,
                "detail": exc.detail,
            },
        )
        return None
    finally:
        await provider.aclose()


def _build_email(quiz: Quiz, recipient: str, stats: results_service.QuizResultStats) -> tuple[str, str]:
    """Return (html, text).

    Deliberately links to the authenticated dashboard rather than embedding a
    public file URL: the workbook holds student names, answers and marks, and
    an emailed link is forwarded, logged and indexed far more easily than a
    session is.
    """
    dashboard = f"{settings.frontend_url.rstrip('/')}/teacher/quizzes/{quiz.id}/results"
    title = html.escape(quiz.title)

    def line(label: str, value: object) -> str:
        return f"<tr><td style='padding:4px 12px 4px 0'>{label}</td><td><strong>{value}</strong></td></tr>"

    average = (
        f"{stats.average_percentage}%" if stats.average_percentage is not None else "not available"
    )
    body = f"""<div style="font-family:system-ui,-apple-system,Segoe UI,sans-serif;color:#0f172a">
  <h2 style="margin:0 0 4px">Results for {title}</h2>
  <p style="margin:0 0 16px;color:#475569">The full workbook is attached.</p>
  <table style="border-collapse:collapse;font-size:14px">
    {line('Attempts started', stats.attempt_count)}
    {line('Finalized', stats.finalized_count)}
    {line('Submitted by student', stats.submitted_count)}
    {line('Auto-submitted (integrity)', stats.auto_submitted_count)}
    {line('Expired', stats.expired_count)}
    {line('Average score', average)}
    {line('Flagged attempts', stats.flagged_count)}
  </table>
  <p style="margin:20px 0">
    <a href="{dashboard}" style="background:#2449d4;color:#fff;padding:10px 16px;border-radius:8px;text-decoration:none">
      Open the results dashboard
    </a>
  </p>
  <p style="font-size:12px;color:#64748b;margin-top:24px">
    Integrity signals are browser-level observations, not proof of misconduct.
    Opening the dashboard requires you to sign in.
  </p>
</div>"""

    text = (
        f"Results for {quiz.title}\n\n"
        f"Attempts started: {stats.attempt_count}\n"
        f"Finalized: {stats.finalized_count}\n"
        f"Submitted by student: {stats.submitted_count}\n"
        f"Auto-submitted (integrity): {stats.auto_submitted_count}\n"
        f"Expired: {stats.expired_count}\n"
        f"Average score: {average}\n"
        f"Flagged attempts: {stats.flagged_count}\n\n"
        f"Dashboard: {dashboard}\n\n"
        "Integrity signals are browser-level observations, not proof of misconduct."
    )
    return body, text


async def send_results_email(
    db: Session, quiz: Quiz, *, requested_by: User | None = None
) -> DeliveryOutcome:
    """Build the export, optionally store it, and email it to the quiz owner.

    Every path through this function leaves the results untouched. The delivery
    row records what happened so the teacher can see it and retry.
    """
    recipient = quiz.owner.email if quiz.owner else None
    if not recipient:
        raise ValueError("Quiz owner has no email address.")

    stats = results_service.quiz_result_stats(db, quiz)

    delivery = ResultDelivery(
        quiz_id=quiz.id,
        requested_by_id=requested_by.id if requested_by else None,
        kind=DeliveryKind.RESULTS_EMAIL,
        status=DeliveryStatus.PENDING,
        recipient=recipient,
        attempt_count=stats.attempt_count,
    )
    db.add(delivery)
    db.commit()
    db.refresh(delivery)

    # Built from database truth, never from a client payload.
    payload = export_service.build_workbook(db, quiz)
    filename = export_service.export_filename(quiz)

    stored_path = await _store_export(quiz, delivery, payload)
    if stored_path:
        delivery.export_object_path = stored_path

    body_html, body_text = _build_email(quiz, recipient, stats)
    provider = get_email_provider()
    delivery.provider = provider.name
    delivery.attempts_made += 1

    try:
        result = await provider.send(
            EmailMessage(
                to=recipient,
                subject=f"Quiz results — {quiz.title}",
                html=body_html,
                text=body_text,
                attachments=(
                    EmailAttachment(
                        filename=filename,
                        content=payload,
                        content_type=export_service.XLSX_CONTENT_TYPE,
                    ),
                ),
            )
        )
    except EmailProviderError as exc:
        # The results are already durable. This records the failure and stops;
        # it does not raise, and it does not touch a single attempt row.
        delivery.status = DeliveryStatus.FAILED
        delivery.failed_at = utcnow()
        delivery.error_code = type(exc).__name__
        delivery.error_detail = (exc.detail or exc.message)[:1000]
        db.commit()
        db.refresh(delivery)

        log_event(
            Event.EMAIL_FAILED,
            quiz_id=str(quiz.id),
            delivery_id=str(delivery.id),
            provider=provider.name,
            error=type(exc).__name__,
            retryable=exc.retryable,
            attempt_count=stats.attempt_count,
        )
        return DeliveryOutcome(
            delivery=delivery,
            sent=False,
            error_code=type(exc).__name__,
            error_message=exc.message,
        )
    finally:
        await provider.aclose()

    delivery.status = DeliveryStatus.SENT
    delivery.sent_at = utcnow()
    delivery.provider_message_id = result.message_id
    delivery.error_code = None
    delivery.error_detail = None
    db.commit()
    db.refresh(delivery)

    log_event(
        Event.EMAIL_SENT,
        quiz_id=str(quiz.id),
        delivery_id=str(delivery.id),
        provider=provider.name,
        message_id=result.message_id,
        attempt_count=stats.attempt_count,
        retained=bool(stored_path),
    )
    return DeliveryOutcome(delivery=delivery, sent=True)


def recent_deliveries(db: Session, quiz: Quiz, *, limit: int = 20) -> list[ResultDelivery]:
    """Delivery history for one quiz, newest first."""
    return list(
        db.scalars(
            select(ResultDelivery)
            .where(ResultDelivery.quiz_id == quiz.id)
            .order_by(ResultDelivery.created_at.desc())
            .limit(limit)
        )
    )


__all__ = ["DeliveryOutcome", "recent_deliveries", "send_results_email"]
