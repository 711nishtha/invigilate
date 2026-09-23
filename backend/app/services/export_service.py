"""XLSX export.

Built entirely from database truth: the workbook reads the same rows the
results dashboard reads, never a client payload. A student who tampers with
their browser changes nothing here, because nothing here consults the browser.

Generated in memory and returned as bytes. Nothing touches the container
filesystem, which on Render is destroyed on every deploy.

Deliberately absent from every sheet: attempt tokens, token hashes, seeds, API
keys, and any data belonging to another quiz.
"""

from __future__ import annotations

import io
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.logging import Event, log_event
from app.db.models import Quiz
from app.services import results_service

HEADER_FILL = PatternFill("solid", fgColor="1D3387")
HEADER_FONT = Font(color="FFFFFF", bold=True)

# Columns that must never appear in an export, asserted by a test. Listing them
# here keeps the rule next to the code that would break it.
FORBIDDEN_EXPORT_FIELDS = frozenset(
    {
        "attempt_token",
        "attempt_token_hash",
        "seed",
        "api_key",
        "service_role",
        "supabase_jwt_secret",
        "content_hash",
    }
)


def _sheet(workbook: Workbook, title: str, headers: list[str]) -> Worksheet:
    sheet = workbook.create_sheet(title=title)
    sheet.append(headers)
    for index, _header in enumerate(headers, start=1):
        cell = sheet.cell(row=1, column=index)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center")
    sheet.freeze_panes = "A2"
    return sheet


def _autosize(sheet: Worksheet, *, maximum: int = 60) -> None:
    """Approximate column widths from content length.

    openpyxl has no real autofit; this keeps a workbook readable without
    forcing the teacher to widen every column by hand.
    """
    for column in sheet.columns:
        longest = 0
        letter = get_column_letter(column[0].column)
        for cell in column:
            value = cell.value
            if value is not None:
                longest = max(longest, len(str(value)))
        sheet.column_dimensions[letter].width = min(max(longest + 2, 10), maximum)


def _naive(value: datetime | None) -> datetime | None:
    """Excel cannot store a timezone-aware datetime.

    Every timestamp in this system is UTC, so dropping tzinfo loses nothing as
    long as the sheet says so — which the Summary sheet does.
    """
    if value is None:
        return None
    return value.replace(tzinfo=None)


def build_workbook(db: Session, quiz: Quiz) -> bytes:
    """Build the four-sheet results workbook for one quiz.

    The caller must already have resolved the quiz through
    ``services.ownership``; this function does not re-check ownership and will
    export whatever quiz it is handed.
    """
    stats = results_service.quiz_result_stats(db, quiz)
    rows = results_service.attempt_rows(db, quiz)
    question_stats = results_service.question_statistics(db, quiz)

    workbook = Workbook()
    # openpyxl creates a default sheet; the Summary replaces it.
    workbook.remove(workbook.active)

    _build_summary(workbook, quiz, stats)
    _build_answers(workbook, db, quiz)
    _build_integrity(workbook, db, quiz)
    _build_question_stats(workbook, question_stats)

    buffer = io.BytesIO()
    workbook.save(buffer)
    payload = buffer.getvalue()

    log_event(
        Event.EXPORT_CREATED,
        quiz_id=str(quiz.id),
        attempt_count=len(rows),
        bytes=len(payload),
        sheets=len(workbook.sheetnames),
    )
    return payload


def _build_summary(workbook: Workbook, quiz: Quiz, stats: results_service.QuizResultStats) -> None:
    sheet = workbook.create_sheet(title="Summary")
    sheet["A1"] = "Secure Quiz — results export"
    sheet["A1"].font = Font(bold=True, size=14)

    def block(start_row: int, title: str, pairs: list[tuple[str, object]]) -> int:
        sheet.cell(row=start_row, column=1, value=title).font = Font(bold=True)
        row = start_row + 1
        for label, value in pairs:
            sheet.cell(row=row, column=1, value=label)
            sheet.cell(row=row, column=2, value=value)
            row += 1
        return row + 1

    row = block(
        3,
        "Quiz",
        [
            ("Title", quiz.title),
            ("Description", quiz.description or ""),
            ("Public code", quiz.public_code or ""),
            ("Status", quiz.status.value),
            ("Questions per student", quiz.question_count),
            ("Duration (minutes)", round(quiz.duration_seconds / 60, 1)),
            ("Security level", quiz.security_level.value),
            ("Violation budget", quiz.max_integrity_violations),
            ("Generated at (UTC)", _naive(utcnow())),
            ("All timestamps in this workbook are UTC", ""),
        ],
    )

    row = block(
        row,
        "Attempts",
        [
            ("Total started", stats.attempt_count),
            ("Still in progress", stats.in_progress_count),
            ("Submitted by student", stats.submitted_count),
            ("Auto-submitted (integrity)", stats.auto_submitted_count),
            ("Expired (time)", stats.expired_count),
            ("Finalized", stats.finalized_count),
            (
                "Completion rate (%)",
                stats.completion_rate if stats.completion_rate is not None else "n/a",
            ),
        ],
    )

    # "n/a" rather than 0 when there is nothing to average: a zero average
    # would read as "everybody scored nothing".
    row = block(
        row,
        "Scores",
        [
            ("Average (%)", stats.average_percentage if stats.average_percentage is not None else "n/a"),
            ("Highest (%)", stats.highest_percentage if stats.highest_percentage is not None else "n/a"),
            ("Lowest (%)", stats.lowest_percentage if stats.lowest_percentage is not None else "n/a"),
            ("Average marks", stats.average_score if stats.average_score is not None else "n/a"),
            ("Marks available", stats.max_score if stats.max_score is not None else "n/a"),
        ],
    )

    row = block(
        row,
        "Integrity",
        [
            ("Clean", stats.clean_count),
            ("Warning", stats.warning_count),
            ("Flagged", stats.flagged_count),
            ("Recorded events", stats.integrity_event_count),
            (
                "Note",
                "Integrity signals are browser-level observations, not proof of misconduct.",
            ),
        ],
    )

    if stats.score_distribution:
        sheet.cell(row=row, column=1, value="Score distribution").font = Font(bold=True)
        row += 1
        for band in stats.score_distribution:
            sheet.cell(row=row, column=1, value=str(band["band"]))
            sheet.cell(row=row, column=2, value=band["count"])
            row += 1

    _autosize(sheet, maximum=80)


def _build_answers(workbook: Workbook, db: Session, quiz: Quiz) -> None:
    sheet = _sheet(
        workbook,
        "Answers",
        [
            "Student",
            "Email",
            "Attempt ID",
            "Attempt status",
            "Position",
            "Topic",
            "Difficulty",
            "Original question",
            "Question as served",
            "Selected answer",
            "Correct answer",
            "Correct",
            "Answered at (UTC)",
        ],
    )

    for row in results_service.attempt_rows(db, quiz):
        attempt = results_service.load_attempt_detail(db, row.attempt_id)
        if attempt is None:  # pragma: no cover - row came from the same query
            continue
        for question in sorted(attempt.questions, key=lambda item: item.position):
            answer = question.answer
            # is_correct is NULL until finalization, so an in-progress attempt
            # shows "not graded" rather than a misleading FALSE.
            if answer is None:
                correct = "unanswered"
            elif answer.is_correct is None:
                correct = "not graded"
            else:
                correct = "yes" if answer.is_correct else "no"

            sheet.append(
                [
                    attempt.student_name,
                    attempt.student_email,
                    str(attempt.id),
                    attempt.status.value,
                    question.position + 1,
                    question.variant.family.topic or "",
                    question.variant.family.difficulty or "",
                    question.variant.family.original_question,
                    question.variant.question_text,
                    results_service.selected_answer_text(question) or "",
                    results_service.canonical_answer_text(question) or "",
                    correct,
                    _naive(answer.answered_at) if answer else None,
                ]
            )

    _autosize(sheet)


def _build_integrity(workbook: Workbook, db: Session, quiz: Quiz) -> None:
    sheet = _sheet(
        workbook,
        "Integrity",
        [
            "Student",
            "Email",
            "Attempt ID",
            "Timestamp (UTC)",
            "Event",
            "Severity",
            "Action taken",
            "Client reported at (UTC)",
            "Metadata",
        ],
    )

    for row in results_service.attempt_rows(db, quiz):
        attempt = results_service.load_attempt_detail(db, row.attempt_id)
        if attempt is None:  # pragma: no cover
            continue
        for event in sorted(attempt.integrity_events, key=lambda item: item.occurred_at):
            sheet.append(
                [
                    attempt.student_name,
                    attempt.student_email,
                    str(attempt.id),
                    _naive(event.occurred_at),
                    event.event_type.value,
                    event.severity,
                    event.action_taken.value,
                    _naive(event.client_reported_at),
                    # Client-supplied and already size-capped on the way in.
                    str(event.metadata_json) if event.metadata_json else "",
                ]
            )

    _autosize(sheet)


def _build_question_stats(workbook: Workbook, stats: list[results_service.QuestionStat]) -> None:
    sheet = _sheet(
        workbook,
        "Question statistics",
        [
            "Question",
            "Topic",
            "Difficulty",
            "Original question",
            "Variant as served",
            "Presented",
            "Answered",
            "Correct",
            "Incorrect",
            "Unanswered",
            "Accuracy (%)",
        ],
    )

    for stat in stats:
        sheet.append(
            [
                stat.family_position + 1,
                stat.topic or "",
                stat.difficulty or "",
                stat.original_question,
                stat.variant_text,
                stat.presented,
                stat.answered,
                stat.correct,
                stat.incorrect,
                stat.unanswered,
                stat.accuracy if stat.accuracy is not None else "n/a",
            ]
        )

    _autosize(sheet)


def export_filename(quiz: Quiz) -> str:
    """A filename a teacher can find again in a downloads folder."""
    safe = "".join(
        character if character.isalnum() or character in " -_" else "_"
        for character in quiz.title
    ).strip()[:60]
    stamp = utcnow().strftime("%Y%m%d-%H%M")
    return f"{safe or 'quiz'}-results-{stamp}.xlsx"


XLSX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)

__all__ = [
    "FORBIDDEN_EXPORT_FIELDS",
    "XLSX_CONTENT_TYPE",
    "build_workbook",
    "export_filename",
]
