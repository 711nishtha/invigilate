"""Attempt creation, paper assembly, delivery and answer saving.

This is the student-facing half of the exam engine. Everything a student's
browser could lie about is decided here instead: which questions they get, in
what order, with options in what order, when the attempt started and when it
ends.

Nothing in this module calls an AI provider. Generation happens during quiz
preparation, so an exam does not depend on a third party being up.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.core.clock import ensure_aware, expiry_from, utcnow
from app.core.errors import AppError, Conflict, NotFound
from app.core.logging import Event, log_event
from app.core.security import (
    generate_attempt_seed,
    generate_attempt_token,
    hash_attempt_token,
)
from app.db.models import (
    Answer,
    Attempt,
    AttemptQuestion,
    AttemptStatus,
    QuestionType,
    Quiz,
    QuizStatus,
    User,
)
from app.services.identity import StudentIdentity, resolve_identity
from app.services.selection import (
    SelectionCriteria,
    build_pools,
    distinct_paper_estimate,
    filter_pools,
    plan_paper,
)


# The alphabet generate_public_code draws from. Anything else cannot be a real
# code, so it is refused without a database round trip.
_PUBLIC_CODE_CHARS = frozenset("ABCDEFGHJKMNPQRSTUVWXYZ23456789")


class QuizNotAvailable(AppError):
    code = "QUIZ_NOT_AVAILABLE"
    status_code = 404


@dataclass(frozen=True)
class StartedAttempt:
    """A freshly created attempt plus the token shown to the student once."""

    attempt: Attempt
    token: str
    question_count: int


def get_public_quiz(db: Session, public_code: str) -> Quiz:
    """Resolve a published quiz by its public code.

    The public code is a discovery mechanism, not authorization: it identifies
    a quiz and grants nothing beyond the pre-exam summary. A quiz that is not
    PUBLISHED is indistinguishable from one that does not exist, so a guessed
    code cannot reveal that a draft quiz is being prepared.
    """
    code = (public_code or "").strip().upper()
    # Validate against the known alphabet before the value reaches a query.
    # A public code is attacker-supplied path input: PostgreSQL rejects a NUL
    # byte outright, so a %00 in the URL would surface as a 500 and a noisy
    # stack trace rather than the 404 every other bad code gets.
    if not code or len(code) > 16 or not set(code) <= _PUBLIC_CODE_CHARS:
        raise QuizNotAvailable("No quiz found for that code.")

    quiz = db.scalar(
        select(Quiz).where(Quiz.public_code == code, Quiz.status == QuizStatus.PUBLISHED)
    )
    if quiz is None:
        raise QuizNotAvailable("No quiz found for that code.")
    return quiz


def _shuffled_options(
    variant_options: list[str] | None, rng: random.Random, enabled: bool
) -> list[int] | None:
    """Permutation mapping displayed position -> canonical option index.

    Stored per attempt so the mapping never reaches the browser. The student
    submits a *displayed* index; grading maps it back. Replaying the request
    with a different index therefore tells an attacker nothing about the key.
    """
    if not variant_options:
        return None
    order = list(range(len(variant_options)))
    if enabled:
        rng.shuffle(order)
    return order


def create_attempt(
    db: Session,
    quiz: Quiz,
    *,
    student_name: str,
    student_email: str,
    signed_in_user: User | None = None,
) -> StartedAttempt:
    """Create an attempt and freeze its paper.

    Order matters: the paper is assembled and persisted in the same transaction
    as the attempt, so an attempt can never exist without the exact questions it
    was given.
    """
    if quiz.status is not QuizStatus.PUBLISHED:
        raise QuizNotAvailable("This quiz is not open for attempts.")

    identity: StudentIdentity = resolve_identity(
        quiz=quiz,
        student_name=student_name,
        student_email=student_email,
        signed_in_user=signed_in_user,
    )

    # Cheap pre-check for a friendly error. The database unique constraint is
    # what actually enforces the rule under a race; see below.
    existing = db.scalar(
        select(Attempt).where(
            Attempt.quiz_id == quiz.id, Attempt.student_email == identity.dedupe_key
        )
    )
    if existing is not None:
        raise Conflict(
            "You have already started this quiz. Each student may attempt it once.",
            code="ATTEMPT_ALREADY_EXISTS",
        )

    pools = build_pools(db, quiz)
    pools = filter_pools(pools, SelectionCriteria.for_quiz(quiz))
    if len(pools) < quiz.question_count:
        # The publish rules should have prevented this; if content changed after
        # publication we refuse rather than hand out a short paper.
        raise QuizNotAvailable(
            "This quiz is not currently ready for attempts. Please contact your teacher."
        )

    # Server-generated. A client-supplied seed would let a student replay
    # another student's paper, or search for an easier one.
    seed = generate_attempt_seed()
    plan = plan_paper(pools, seed=seed, question_count=quiz.question_count)
    if len(plan) != quiz.question_count:
        raise QuizNotAvailable("This quiz could not produce a complete paper.")

    started_at = utcnow()
    token = generate_attempt_token()

    attempt = Attempt(
        quiz_id=quiz.id,
        student_user_id=signed_in_user.id if signed_in_user else None,
        student_name=identity.display_name,
        student_email=identity.dedupe_key,
        attempt_token_hash=hash_attempt_token(token),
        started_at=started_at,
        expires_at=expiry_from(started_at, quiz.duration_seconds),
        seed=seed,
        status=AttemptStatus.IN_PROGRESS,
    )
    db.add(attempt)

    try:
        db.flush()
    except IntegrityError as exc:
        # UNIQUE (quiz_id, student_email). Two simultaneous starts both passed
        # the pre-check; the database settles it and the loser is told plainly.
        db.rollback()
        raise Conflict(
            "You have already started this quiz. Each student may attempt it once.",
            code="ATTEMPT_ALREADY_EXISTS",
        ) from exc

    # Order and option permutations are drawn from the same seed, so the whole
    # paper is reproducible from the seed stored on the attempt row.
    rng = random.Random(seed)
    variants_by_id = _load_variants(db, [variant_id for _, variant_id in plan])

    positions = list(range(len(plan)))
    if quiz.randomization_enabled:
        rng.shuffle(positions)

    for position, (_family_id, variant_id) in zip(positions, plan, strict=True):
        variant = variants_by_id[variant_id]
        db.add(
            AttemptQuestion(
                attempt_id=attempt.id,
                variant_id=variant_id,
                position=position,
                options_order_json=_shuffled_options(
                    variant.options_json, rng, quiz.randomization_enabled
                ),
            )
        )

    db.commit()
    db.refresh(attempt)

    log_event(
        Event.ATTEMPT_STARTED,
        attempt_id=str(attempt.id),
        quiz_id=str(quiz.id),
        question_count=len(plan),
        # The seed explains the paper after the fact. It is not a secret: it is
        # useless without the pool, and the paper is already frozen.
        seed=str(seed),
        identity_mechanism=identity.mechanism.value,
        identity_verified=identity.is_verified,
        pool_estimate=distinct_paper_estimate(pools, quiz.question_count),
    )
    return StartedAttempt(attempt=attempt, token=token, question_count=len(plan))


def _load_variants(db: Session, variant_ids: list[uuid.UUID]) -> dict[uuid.UUID, Any]:
    from app.db.models import QuestionVariant

    rows = db.scalars(
        select(QuestionVariant).where(QuestionVariant.id.in_(variant_ids))
    ).all()
    return {row.id: row for row in rows}


def load_paper(db: Session, attempt: Attempt) -> list[AttemptQuestion]:
    """The frozen paper, in this student's order.

    Reads ``attempt_questions`` and never re-plans. A refresh therefore returns
    the identical paper, and a teacher editing the question afterwards cannot
    change what this attempt shows: the row references the exact variant that
    was served, and an approved variant is superseded rather than rewritten.
    """
    return list(
        db.scalars(
            select(AttemptQuestion)
            .where(AttemptQuestion.attempt_id == attempt.id)
            .options(
                selectinload(AttemptQuestion.variant),
                selectinload(AttemptQuestion.answer),
            )
            .order_by(AttemptQuestion.position)
        )
    )


def displayed_options(attempt_question: AttemptQuestion) -> list[str] | None:
    """Options in this student's order.

    Built by walking the stored permutation, so the displayed order is stable
    across refreshes and identical to what was first served.
    """
    variant = attempt_question.variant
    if not variant.options_json:
        return None
    order = attempt_question.options_order_json
    if not order:
        return list(variant.options_json)
    return [variant.options_json[index] for index in order]


# --- Answer saving ---------------------------------------------------------


def _validate_selected_answer(
    attempt_question: AttemptQuestion, payload: dict[str, Any]
) -> dict[str, Any]:
    """Accept only a well-formed answer for this question's type.

    Grading fields are not merely ignored — they are rejected, so a client that
    tries to submit ``is_correct`` learns that the server decides that, rather
    than silently believing it worked.
    """
    forbidden = {"is_correct", "score", "percentage", "correct", "correct_option", "grade"}
    present = forbidden & set(payload)
    if present:
        raise AppError(
            "Correctness is decided by the server and cannot be supplied.",
            code="GRADING_FIELD_REJECTED",
            details={"fields": sorted(present)},
        )

    question_type = attempt_question.variant.family.question_type

    if question_type in (QuestionType.MCQ, QuestionType.TRUE_FALSE):
        index = payload.get("displayed_index")
        if isinstance(index, bool) or not isinstance(index, int):
            raise AppError(
                "An answer must be the index of the option you selected.",
                code="INVALID_ANSWER_FORMAT",
            )
        options = displayed_options(attempt_question) or []
        if not 0 <= index < len(options):
            raise AppError(
                "That option does not exist for this question.",
                code="OPTION_OUT_OF_RANGE",
                details={"option_count": len(options)},
            )
        return {"displayed_index": index}

    if question_type is QuestionType.NUMERICAL:
        raw = payload.get("value")
        if raw is None or isinstance(raw, bool):
            raise AppError("A numeric answer is required.", code="INVALID_ANSWER_FORMAT")
        try:
            # Stored as text so the exact digits the student typed survive; the
            # Decimal comparison at grading time reads this back.
            float(str(raw))
        except (TypeError, ValueError):
            raise AppError(
                "That is not a number.", code="INVALID_ANSWER_FORMAT"
            ) from None
        return {"value": str(raw)}

    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise AppError("An answer is required.", code="INVALID_ANSWER_FORMAT")
    return {"text": text.strip()[:4000]}


def save_answer(
    db: Session,
    attempt: Attempt,
    attempt_question_id: str | uuid.UUID,
    selected_answer: dict[str, Any],
) -> Answer:
    """Persist one answer, upserting over any previous one.

    The question must belong to *this* attempt. Looking it up scoped to the
    attempt means a student cannot write into someone else's paper by supplying
    its question id — the row simply is not found.
    """
    try:
        question_id = (
            attempt_question_id
            if isinstance(attempt_question_id, uuid.UUID)
            else uuid.UUID(str(attempt_question_id))
        )
    except (ValueError, AttributeError, TypeError):
        raise NotFound("No such question on this attempt.", code="QUESTION_NOT_FOUND") from None

    attempt_question = db.scalar(
        select(AttemptQuestion)
        .where(
            AttemptQuestion.id == question_id,
            AttemptQuestion.attempt_id == attempt.id,
        )
        .options(
            selectinload(AttemptQuestion.variant),
            selectinload(AttemptQuestion.answer),
        )
    )
    if attempt_question is None:
        raise NotFound("No such question on this attempt.", code="QUESTION_NOT_FOUND")

    cleaned = _validate_selected_answer(attempt_question, selected_answer)

    answer = attempt_question.answer
    if answer is None:
        answer = Answer(
            attempt_question_id=attempt_question.id,
            selected_answer=cleaned,
            answered_at=utcnow(),
        )
        db.add(answer)
    else:
        answer.selected_answer = cleaned
        answer.answered_at = utcnow()
    # is_correct is deliberately left untouched. Correctness does not exist in
    # the database until finalization, so it cannot leak mid-exam.

    db.commit()
    db.refresh(answer)

    log_event(
        Event.ANSWER_SUBMITTED,
        attempt_id=str(attempt.id),
        attempt_question_id=str(attempt_question.id),
        position=attempt_question.position,
        # The answer itself is the student's data and adds nothing to an audit
        # trail that already records which question was answered and when.
    )
    return answer


def answered_count(questions: list[AttemptQuestion]) -> int:
    return sum(1 for question in questions if question.answer is not None)


def seconds_remaining_for(attempt: Attempt) -> int:
    from app.core.clock import seconds_remaining

    return seconds_remaining(ensure_aware(attempt.expires_at))


__all__ = [
    "QuizNotAvailable",
    "StartedAttempt",
    "answered_count",
    "create_attempt",
    "displayed_options",
    "get_public_quiz",
    "load_paper",
    "save_answer",
    "seconds_remaining_for",
]
