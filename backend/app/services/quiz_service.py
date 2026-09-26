"""Quiz lifecycle: creation, settings, publication.

The publish/unpublish rules here are the ones that protect students mid-exam.
They are enforced server-side and nowhere else.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.errors import AppError, Conflict
from app.core.logging import Event, log_event
from app.core.security import generate_public_code
from app.db.models import Attempt, QuestionVariant, Quiz, QuizStatus, User, VariantStatus
from app.services import proctoring_policy
from app.services.publish_rules import PublishReadiness, evaluate_publish_readiness

# Settings that change the exam a student is sitting. Locked once a quiz is
# live, because altering the duration or question count underneath an
# in-progress attempt would corrupt it.
EXAM_SHAPE_FIELDS = frozenset(
    {
        "duration_seconds",
        "question_count",
        "security_level",
        "randomization_enabled",
        "allow_backtracking",
        "max_integrity_violations",
        "coverage_config_json",
        # Proctoring is part of the exam's shape. A student who passed a
        # pre-exam check for a PROCTORED quiz must not find themselves in a
        # differently-monitored exam because the teacher edited it while the
        # link was live.
        "proctoring_mode",
        "proctoring_config_json",
    }
)

_PUBLIC_CODE_ATTEMPTS = 10


def create_quiz(db: Session, owner: User, data: dict[str, Any]) -> Quiz:
    quiz = Quiz(owner_id=owner.id, status=QuizStatus.DRAFT, **data)
    db.add(quiz)
    db.commit()
    db.refresh(quiz)
    log_event(Event.QUIZ_CREATED, quiz_id=str(quiz.id), owner_id=str(owner.id))
    return quiz


def update_quiz(db: Session, quiz: Quiz, changes: dict[str, Any]) -> Quiz:
    """Apply setting changes, refusing any that would alter a live exam."""
    if quiz.status is QuizStatus.PUBLISHED:
        locked = sorted(EXAM_SHAPE_FIELDS & changes.keys())
        if locked:
            raise Conflict(
                "These settings cannot change while the quiz is published. "
                "Unpublish it first.",
                code="QUIZ_PUBLISHED_IMMUTABLE_FIELDS",
                details={"fields": locked},
            )
    if quiz.status in (QuizStatus.CLOSED, QuizStatus.ARCHIVED):
        raise Conflict(
            f"A {quiz.status.value.lower()} quiz cannot be edited.",
            code="QUIZ_NOT_EDITABLE",
        )

    # The policy has to be validated against the *resulting* state, not the
    # submitted fragment: sending only `proctoring_config_json` must be checked
    # against the mode already on the row, and vice versa.
    if "proctoring_mode" in changes or "proctoring_config_json" in changes:
        mode = changes.get("proctoring_mode", quiz.proctoring_mode)
        config = changes.get("proctoring_config_json", quiz.proctoring_config_json or {})
        try:
            proctoring_policy.validate_config(mode, config)
        except ValueError as exc:
            raise AppError(
                str(exc), code="PROCTORING_CONFIG_INVALID", status_code=422
            ) from exc

    for field, value in changes.items():
        setattr(quiz, field, value)
    db.commit()
    db.refresh(quiz)
    return quiz


def _allocate_public_code(db: Session) -> str:
    """A unique, non-sequential code.

    Retried on collision rather than trusted blindly: the space is large but a
    duplicate would silently point two quizzes at one URL.
    """
    for _ in range(_PUBLIC_CODE_ATTEMPTS):
        code = generate_public_code()
        if db.scalar(select(Quiz.id).where(Quiz.public_code == code)) is None:
            return code
    raise Conflict(
        "Could not allocate a unique quiz code. Please try again.",
        code="PUBLIC_CODE_ALLOCATION_FAILED",
    )


def publish_quiz(db: Session, quiz: Quiz) -> tuple[Quiz, PublishReadiness]:
    """Publish, or refuse with the specific reasons.

    Publishing also promotes every assignable variant to PUBLISHED, which is
    what makes them immutable from this point on.
    """
    if quiz.status is QuizStatus.PUBLISHED:
        # Idempotent: re-publishing a live quiz is not an error.
        return quiz, evaluate_publish_readiness(db, quiz)
    if quiz.status in (QuizStatus.CLOSED, QuizStatus.ARCHIVED):
        raise Conflict(
            f"A {quiz.status.value.lower()} quiz cannot be published again.",
            code="QUIZ_NOT_PUBLISHABLE",
        )

    readiness = evaluate_publish_readiness(db, quiz)
    if not readiness.publishable:
        raise Conflict(
            "This quiz is not ready to publish.",
            code="QUIZ_NOT_READY",
            details={"blockers": [b.to_json() for b in readiness.blockers]},
        )

    if quiz.public_code is None:
        quiz.public_code = _allocate_public_code(db)

    for family in quiz.question_families:
        for variant in family.variants:
            if variant.validation_status is VariantStatus.APPROVED and not variant.superseded_by_id:
                variant.validation_status = VariantStatus.PUBLISHED

    quiz.status = QuizStatus.PUBLISHED
    quiz.published_at = utcnow()
    db.commit()
    db.refresh(quiz)

    log_event(
        Event.QUIZ_PUBLISHED,
        quiz_id=str(quiz.id),
        public_code=quiz.public_code,
        question_count=quiz.question_count,
        ready_families=readiness.ready_family_count,
        assignable_variants=readiness.assignable_variant_count,
    )
    return quiz, readiness


def unpublish_quiz(db: Session, quiz: Quiz) -> Quiz:
    """Take a quiz offline, but only while nobody has sat it.

    Once an attempt exists the quiz is part of somebody's record, so the exit
    is CLOSED (no new attempts, existing ones finish) rather than a return to
    DRAFT. Variants already promoted to PUBLISHED stay there: they may have
    been served, and a served question must never become editable again.
    """
    if quiz.status is not QuizStatus.PUBLISHED:
        raise Conflict(
            "Only a published quiz can be unpublished.", code="QUIZ_NOT_PUBLISHED"
        )

    attempt_count = db.scalar(
        select(func.count()).select_from(Attempt).where(Attempt.quiz_id == quiz.id)
    )
    if attempt_count:
        raise Conflict(
            "Students have already started this quiz, so it cannot be unpublished. "
            "Close it instead to stop new attempts.",
            code="QUIZ_HAS_ATTEMPTS",
            details={"attempt_count": int(attempt_count)},
        )

    # The public code is kept so republishing reuses the same link. The quiz is
    # unreachable while it is not PUBLISHED, which the public lookup enforces.
    quiz.status = QuizStatus.DRAFT
    quiz.published_at = None
    db.commit()
    db.refresh(quiz)
    log_event("QUIZ_UNPUBLISHED", quiz_id=str(quiz.id))
    return quiz


def close_quiz(db: Session, quiz: Quiz) -> Quiz:
    """Stop new attempts. In-flight attempts are unaffected and still finish."""
    if quiz.status not in (QuizStatus.PUBLISHED, QuizStatus.DRAFT):
        raise Conflict(
            f"A {quiz.status.value.lower()} quiz cannot be closed.", code="QUIZ_NOT_CLOSEABLE"
        )
    quiz.status = QuizStatus.CLOSED
    db.commit()
    db.refresh(quiz)
    log_event("QUIZ_CLOSED", quiz_id=str(quiz.id))
    return quiz


def quiz_stats(db: Session, quiz: Quiz) -> dict[str, int]:
    """Counters for the quiz detail screen. Computed, never stored."""
    family_ids = [family.id for family in quiz.question_families]
    variant_counts: dict[str, int] = {status.value: 0 for status in VariantStatus}
    if family_ids:
        rows = db.execute(
            select(QuestionVariant.validation_status, func.count())
            .where(QuestionVariant.family_id.in_(family_ids))
            .group_by(QuestionVariant.validation_status)
        ).all()
        for status, count in rows:
            variant_counts[status.value] = int(count)

    attempt_count = db.scalar(
        select(func.count()).select_from(Attempt).where(Attempt.quiz_id == quiz.id)
    )
    return {
        "family_count": len(family_ids),
        "attempt_count": int(attempt_count or 0),
        **{f"variants_{key.lower()}": value for key, value in variant_counts.items()},
    }


__all__ = [
    "EXAM_SHAPE_FIELDS",
    "close_quiz",
    "create_quiz",
    "publish_quiz",
    "quiz_stats",
    "unpublish_quiz",
    "update_quiz",
]
