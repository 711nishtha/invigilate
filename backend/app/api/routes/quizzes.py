"""Teacher quiz routes.

Every handler resolves its quiz through ``services.ownership``, which scopes
the query to the caller. No handler queries ``quizzes`` directly, so no handler
can forget the ownership predicate.
"""

# NOTE: no `from __future__ import annotations` here on purpose. Rate-limited
# handlers are wrapped by slowapi, and FastAPI then resolves their annotations
# against the wrapper's module globals, where our dependency aliases do not
# exist. Real annotation objects sidestep that entirely.
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response, status
from sqlalchemy import func, select

from app.api.deps import CurrentTeacher, DbSession
from app.core.rate_limit import limiter
from app.db.models import Attempt, QuestionFamily, QuestionVariant, Quiz, VariantStatus
from app.schemas.quiz import (
    PublishReadinessOut,
    QuizCreateRequest,
    QuizDetailOut,
    QuizSummaryOut,
    QuizUpdateRequest,
)
from app.services import quiz_service
from app.services.ownership import get_owned_quiz
from app.services.publish_rules import PublishReadiness, evaluate_publish_readiness

router = APIRouter(prefix="/quizzes", tags=["quizzes"])


def _readiness_out(readiness: PublishReadiness) -> PublishReadinessOut:
    return PublishReadinessOut(
        publishable=readiness.publishable,
        blockers=[b.to_json() for b in readiness.blockers],  # type: ignore[arg-type]
        family_count=readiness.family_count,
        ready_family_count=readiness.ready_family_count,
        assignable_variant_count=readiness.assignable_variant_count,
        question_count=readiness.question_count,
        distinct_paper_estimate=readiness.distinct_paper_estimate,
    )


def _detail(db: DbSession, quiz: Quiz) -> QuizDetailOut:
    readiness = evaluate_publish_readiness(db, quiz)
    return QuizDetailOut(
        **{field: getattr(quiz, field) for field in QuizDetailOut.model_fields if field not in ("readiness", "stats")},
        readiness=_readiness_out(readiness),
        stats=quiz_service.quiz_stats(db, quiz),
    )


@router.post("", response_model=QuizDetailOut, status_code=status.HTTP_201_CREATED)
def create_quiz(
    payload: QuizCreateRequest, teacher: CurrentTeacher, db: DbSession
) -> QuizDetailOut:
    quiz = quiz_service.create_quiz(db, teacher, payload.model_dump())
    return _detail(db, quiz)


@router.get("", response_model=list[QuizSummaryOut])
def list_quizzes(
    teacher: CurrentTeacher,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[QuizSummaryOut]:
    """Quizzes owned by the caller, newest first.

    Counters are aggregated in the query rather than by walking relationships,
    so a teacher with many quizzes does not trigger a query per row.
    """
    quizzes = list(
        db.scalars(
            select(Quiz)
            .where(Quiz.owner_id == teacher.id)
            .order_by(Quiz.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    )
    if not quizzes:
        return []

    quiz_ids = [quiz.id for quiz in quizzes]
    family_counts = dict(
        db.execute(
            select(QuestionFamily.quiz_id, func.count())
            .where(QuestionFamily.quiz_id.in_(quiz_ids))
            .group_by(QuestionFamily.quiz_id)
        ).all()
    )
    attempt_counts = dict(
        db.execute(
            select(Attempt.quiz_id, func.count())
            .where(Attempt.quiz_id.in_(quiz_ids))
            .group_by(Attempt.quiz_id)
        ).all()
    )
    variant_counts = dict(
        db.execute(
            select(QuestionFamily.quiz_id, func.count())
            .join(QuestionVariant, QuestionVariant.family_id == QuestionFamily.id)
            .where(
                QuestionFamily.quiz_id.in_(quiz_ids),
                QuestionVariant.validation_status.in_(
                    [VariantStatus.APPROVED, VariantStatus.PUBLISHED]
                ),
                QuestionVariant.superseded_by_id.is_(None),
            )
            .group_by(QuestionFamily.quiz_id)
        ).all()
    )

    return [
        QuizSummaryOut(
            **{field: getattr(quiz, field) for field in QuizSummaryOut.model_fields
               if field not in ("family_count", "attempt_count", "assignable_variant_count")},
            family_count=int(family_counts.get(quiz.id, 0)),
            attempt_count=int(attempt_counts.get(quiz.id, 0)),
            assignable_variant_count=int(variant_counts.get(quiz.id, 0)),
        )
        for quiz in quizzes
    ]


@router.get("/{quiz_id}", response_model=QuizDetailOut)
def get_quiz(quiz_id: str, teacher: CurrentTeacher, db: DbSession) -> QuizDetailOut:
    return _detail(db, get_owned_quiz(db, quiz_id, teacher, with_families=True))


@router.patch("/{quiz_id}", response_model=QuizDetailOut)
def update_quiz(
    quiz_id: str, payload: QuizUpdateRequest, teacher: CurrentTeacher, db: DbSession
) -> QuizDetailOut:
    quiz = get_owned_quiz(db, quiz_id, teacher, with_families=True)
    quiz_service.update_quiz(db, quiz, payload.changes())
    return _detail(db, quiz)


@router.get("/{quiz_id}/readiness", response_model=PublishReadinessOut)
def get_readiness(quiz_id: str, teacher: CurrentTeacher, db: DbSession) -> PublishReadinessOut:
    """Why this quiz can or cannot be published.

    Separate from publishing so the UI can explain the blockers without the
    teacher having to attempt a publish and read an error.
    """
    quiz = get_owned_quiz(db, quiz_id, teacher, with_families=True)
    return _readiness_out(evaluate_publish_readiness(db, quiz))


@router.post("/{quiz_id}/publish", response_model=QuizDetailOut)
@limiter.limit("30/minute")
def publish_quiz(
    request: Request,
    response: Response,  # slowapi injects rate-limit headers into this
    quiz_id: str,
    teacher: CurrentTeacher,
    db: DbSession,
) -> QuizDetailOut:
    quiz = get_owned_quiz(db, quiz_id, teacher, with_families=True)
    quiz, _ = quiz_service.publish_quiz(db, quiz)
    return _detail(db, quiz)


@router.post("/{quiz_id}/unpublish", response_model=QuizDetailOut)
def unpublish_quiz(quiz_id: str, teacher: CurrentTeacher, db: DbSession) -> QuizDetailOut:
    quiz = get_owned_quiz(db, quiz_id, teacher, with_families=True)
    return _detail(db, quiz_service.unpublish_quiz(db, quiz))


@router.post("/{quiz_id}/close", response_model=QuizDetailOut)
def close_quiz(quiz_id: str, teacher: CurrentTeacher, db: DbSession) -> QuizDetailOut:
    quiz = get_owned_quiz(db, quiz_id, teacher, with_families=True)
    return _detail(db, quiz_service.close_quiz(db, quiz))


@router.delete("/{quiz_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_quiz(quiz_id: str, teacher: CurrentTeacher, db: DbSession) -> None:
    """Delete a quiz that nobody has sat.

    A quiz with attempts is part of students' records; it is closed or
    archived rather than destroyed.
    """
    from app.core.errors import Conflict

    quiz = get_owned_quiz(db, quiz_id, teacher)
    attempts = db.scalar(
        select(func.count()).select_from(Attempt).where(Attempt.quiz_id == quiz.id)
    )
    if attempts:
        raise Conflict(
            "Students have attempted this quiz, so it cannot be deleted.",
            code="QUIZ_HAS_ATTEMPTS",
            details={"attempt_count": int(attempts)},
        )
    db.delete(quiz)
    db.commit()


__all__ = ["router"]
