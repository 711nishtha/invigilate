"""Teacher-facing results: statistics and attempt detail.

Every figure here is computed from the database at read time. Nothing is
cached, nothing is denormalized onto the quiz row, and nothing comes from a
client payload — a teacher looking at a score is looking at what grading
actually stored.

Ownership is not enforced here: callers resolve the quiz through
``services.ownership`` first, so these functions only ever see a quiz the
caller already owns.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.core.clock import ensure_aware
from app.db.models import (
    Answer,
    Attempt,
    AttemptQuestion,
    AttemptStatus,
    IntegrityEvent,
    IntegrityStatus,
    QuestionFamily,
    QuestionVariant,
    Quiz,
)

# Score buckets for the distribution chart, as (label, lower, upper) with the
# upper bound inclusive only for the last band.
SCORE_BANDS = (
    ("0-19%", 0, 20),
    ("20-39%", 20, 40),
    ("40-59%", 40, 60),
    ("60-79%", 60, 80),
    ("80-100%", 80, 101),
)


@dataclass
class QuizResultStats:
    """Everything the results dashboard shows for one quiz.

    All counters are real. When there are no attempts every figure is zero and
    the averages are ``None`` — the UI renders an empty state rather than a
    fabricated zero score.
    """

    attempt_count: int = 0
    in_progress_count: int = 0
    submitted_count: int = 0
    auto_submitted_count: int = 0
    expired_count: int = 0
    finalized_count: int = 0

    average_percentage: float | None = None
    highest_percentage: float | None = None
    lowest_percentage: float | None = None
    average_score: float | None = None
    max_score: float | None = None

    clean_count: int = 0
    warning_count: int = 0
    flagged_count: int = 0
    integrity_event_count: int = 0

    score_distribution: list[dict[str, object]] = field(default_factory=list)

    @property
    def completion_rate(self) -> float | None:
        """Share of started attempts that reached a terminal state."""
        if not self.attempt_count:
            return None
        return round(self.finalized_count / self.attempt_count * 100, 1)


def quiz_result_stats(db: Session, quiz: Quiz) -> QuizResultStats:
    """Aggregate statistics for a quiz the caller owns."""
    stats = QuizResultStats()

    attempts = list(db.scalars(select(Attempt).where(Attempt.quiz_id == quiz.id)))
    stats.attempt_count = len(attempts)
    if not attempts:
        # No data is not zero data. Everything stays None/0 and the UI shows an
        # empty state.
        return stats

    by_status = {
        AttemptStatus.IN_PROGRESS: 0,
        AttemptStatus.SUBMITTED: 0,
        AttemptStatus.AUTO_SUBMITTED: 0,
        AttemptStatus.TIME_EXPIRED: 0,
        AttemptStatus.ABANDONED: 0,
    }
    integrity_counts = {
        IntegrityStatus.CLEAN: 0,
        IntegrityStatus.WARNING: 0,
        IntegrityStatus.FLAGGED: 0,
    }
    percentages: list[float] = []
    scores: list[float] = []

    for attempt in attempts:
        by_status[attempt.status] = by_status.get(attempt.status, 0) + 1
        integrity_counts[attempt.integrity_status] = (
            integrity_counts.get(attempt.integrity_status, 0) + 1
        )
        if attempt.is_terminal and attempt.percentage is not None:
            percentages.append(float(attempt.percentage))
        if attempt.is_terminal and attempt.score is not None:
            scores.append(float(attempt.score))

    stats.in_progress_count = by_status[AttemptStatus.IN_PROGRESS]
    stats.submitted_count = by_status[AttemptStatus.SUBMITTED]
    stats.auto_submitted_count = by_status[AttemptStatus.AUTO_SUBMITTED]
    stats.expired_count = by_status[AttemptStatus.TIME_EXPIRED]
    stats.finalized_count = (
        stats.submitted_count + stats.auto_submitted_count + stats.expired_count
    )

    stats.clean_count = integrity_counts[IntegrityStatus.CLEAN]
    stats.warning_count = integrity_counts[IntegrityStatus.WARNING]
    stats.flagged_count = integrity_counts[IntegrityStatus.FLAGGED]

    attempt_ids = [attempt.id for attempt in attempts]
    stats.integrity_event_count = int(
        db.scalar(
            select(func.count())
            .select_from(IntegrityEvent)
            .where(IntegrityEvent.attempt_id.in_(attempt_ids))
        )
        or 0
    )

    if percentages:
        stats.average_percentage = round(sum(percentages) / len(percentages), 2)
        stats.highest_percentage = round(max(percentages), 2)
        stats.lowest_percentage = round(min(percentages), 2)
        stats.score_distribution = [
            {
                "band": label,
                "count": sum(1 for value in percentages if lower <= value < upper),
            }
            for label, lower, upper in SCORE_BANDS
        ]
    if scores:
        stats.average_score = round(sum(scores) / len(scores), 2)
        stats.max_score = max(
            (float(a.max_score) for a in attempts if a.max_score is not None), default=None
        )

    return stats


@dataclass
class AttemptRow:
    """One row of the results table."""

    attempt_id: uuid.UUID
    student_name: str
    student_email: str
    status: AttemptStatus
    started_at: object
    submitted_at: object
    duration_seconds: int | None
    score: float | None
    max_score: float | None
    percentage: float | None
    integrity_status: IntegrityStatus
    violation_count: int
    answered_count: int
    question_count: int


def attempt_rows(db: Session, quiz: Quiz) -> list[AttemptRow]:
    """Result rows for every attempt on this quiz, newest first."""
    attempts = list(
        db.scalars(
            select(Attempt)
            .where(Attempt.quiz_id == quiz.id)
            .options(selectinload(Attempt.questions).selectinload(AttemptQuestion.answer))
            .order_by(Attempt.created_at.desc())
        )
    )

    rows: list[AttemptRow] = []
    for attempt in attempts:
        questions = list(attempt.questions)
        answered = sum(1 for question in questions if question.answer is not None)
        duration = None
        if attempt.submitted_at is not None:
            duration = int(
                (
                    ensure_aware(attempt.submitted_at) - ensure_aware(attempt.started_at)
                ).total_seconds()
            )
        rows.append(
            AttemptRow(
                attempt_id=attempt.id,
                student_name=attempt.student_name,
                student_email=attempt.student_email,
                status=attempt.status,
                started_at=ensure_aware(attempt.started_at),
                submitted_at=(
                    ensure_aware(attempt.submitted_at) if attempt.submitted_at else None
                ),
                duration_seconds=duration,
                score=float(attempt.score) if attempt.score is not None else None,
                max_score=float(attempt.max_score) if attempt.max_score is not None else None,
                percentage=float(attempt.percentage) if attempt.percentage is not None else None,
                integrity_status=attempt.integrity_status,
                violation_count=attempt.integrity_violation_count,
                answered_count=answered,
                question_count=len(questions),
            )
        )
    return rows


def load_attempt_detail(db: Session, attempt_id: uuid.UUID) -> Attempt | None:
    """One attempt with its paper, answers and timeline eagerly loaded."""
    return db.scalar(
        select(Attempt)
        .where(Attempt.id == attempt_id)
        .options(
            selectinload(Attempt.questions)
            .selectinload(AttemptQuestion.variant)
            .selectinload(QuestionVariant.family),
            selectinload(Attempt.questions).selectinload(AttemptQuestion.answer),
            selectinload(Attempt.integrity_events),
        )
    )


def canonical_answer_text(question: AttemptQuestion) -> str | None:
    """The correct answer, in words, for a teacher's eyes.

    Teacher-only: this resolves the stored key. It must never reach a
    student-facing schema.
    """
    variant = question.variant
    if variant.options_json and variant.correct_option is not None:
        if 0 <= variant.correct_option < len(variant.options_json):
            return variant.options_json[variant.correct_option]
        return None
    if variant.numeric_answer_json:
        value = variant.numeric_answer_json.get("value")
        unit = variant.numeric_answer_json.get("unit") or ""
        return f"{value}{(' ' + unit) if unit else ''}".strip() if value is not None else None
    return None


def selected_answer_text(question: AttemptQuestion) -> str | None:
    """What the student chose, rendered through this attempt's option order."""
    answer = question.answer
    if answer is None or not isinstance(answer.selected_answer, dict):
        return None

    payload = answer.selected_answer
    if "displayed_index" in payload:
        index = payload["displayed_index"]
        variant = question.variant
        if not variant.options_json or not isinstance(index, int):
            return str(index)
        order = question.options_order_json or list(range(len(variant.options_json)))
        if 0 <= index < len(order):
            canonical = order[index]
            if 0 <= canonical < len(variant.options_json):
                return variant.options_json[canonical]
        return str(index)
    if "value" in payload:
        return str(payload["value"])
    if "text" in payload:
        return str(payload["text"])
    return None


@dataclass
class QuestionStat:
    """Per-variant aggregate for the question statistics sheet."""

    family_id: uuid.UUID
    variant_id: uuid.UUID
    family_position: int
    original_question: str
    variant_text: str
    topic: str | None
    difficulty: str | None
    presented: int = 0
    answered: int = 0
    correct: int = 0
    incorrect: int = 0

    @property
    def unanswered(self) -> int:
        return self.presented - self.answered

    @property
    def accuracy(self) -> float | None:
        """Accuracy over *answered* questions.

        Unanswered questions are excluded: a question nobody reached says
        nothing about whether it was well written.
        """
        if not self.answered:
            return None
        return round(self.correct / self.answered * 100, 1)


def question_statistics(db: Session, quiz: Quiz) -> list[QuestionStat]:
    """How each served variant performed across every attempt on this quiz."""
    rows = list(
        db.execute(
            select(AttemptQuestion, Answer, QuestionVariant, QuestionFamily)
            .join(Attempt, AttemptQuestion.attempt_id == Attempt.id)
            .join(QuestionVariant, AttemptQuestion.variant_id == QuestionVariant.id)
            .join(QuestionFamily, QuestionVariant.family_id == QuestionFamily.id)
            .outerjoin(Answer, Answer.attempt_question_id == AttemptQuestion.id)
            .where(Attempt.quiz_id == quiz.id)
            .order_by(QuestionFamily.position)
        )
    )

    stats: dict[uuid.UUID, QuestionStat] = {}
    for _attempt_question, answer, variant, family in rows:
        stat = stats.get(variant.id)
        if stat is None:
            stat = QuestionStat(
                family_id=family.id,
                variant_id=variant.id,
                family_position=family.position,
                original_question=family.original_question,
                variant_text=variant.question_text,
                topic=family.topic,
                difficulty=family.difficulty,
            )
            stats[variant.id] = stat

        stat.presented += 1
        if answer is not None:
            stat.answered += 1
            # is_correct is NULL until finalization, so an in-progress attempt
            # counts as answered but neither correct nor incorrect.
            if answer.is_correct is True:
                stat.correct += 1
            elif answer.is_correct is False:
                stat.incorrect += 1

    return sorted(stats.values(), key=lambda item: (item.family_position, str(item.variant_id)))


def decimal_or_none(value: Decimal | float | None) -> float | None:
    return float(value) if value is not None else None


__all__ = [
    "AttemptRow",
    "QuestionStat",
    "QuizResultStats",
    "attempt_rows",
    "canonical_answer_text",
    "load_attempt_detail",
    "question_statistics",
    "quiz_result_stats",
    "selected_answer_text",
]
