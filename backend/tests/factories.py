"""Test data builders.

Kept separate from the tests so that an adversarial test reads as the attack it
describes rather than as twenty lines of setup.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.security import generate_attempt_seed, generate_attempt_token, hash_attempt_token
from app.db.models import (
    Answer,
    Attempt,
    AttemptQuestion,
    QuestionFamily,
    QuestionType,
    QuestionVariant,
    Quiz,
    QuizStatus,
    User,
    UserRole,
    ValidationStatus,
)

OPTIONS = ["O(1)", "O(log n)", "O(n)", "O(n log n)"]


def make_user(db: Session, email: str = "teacher@example.edu", role: UserRole = UserRole.TEACHER) -> User:
    user = User(
        supabase_user_id=str(uuid.uuid4()), name="Dr Rao", email=email.lower(), role=role
    )
    db.add(user)
    db.commit()
    return user


def make_quiz(db: Session, owner: User, **kwargs) -> Quiz:  # type: ignore[no-untyped-def]
    defaults = dict(
        title="Data Structures Midterm",
        duration_seconds=1200,
        question_count=2,
        status=QuizStatus.PUBLISHED,
        public_code=None,
    )
    defaults.update(kwargs)
    quiz = Quiz(owner_id=owner.id, **defaults)  # type: ignore[arg-type]
    db.add(quiz)
    db.commit()
    return quiz


def make_family(
    db: Session,
    quiz: Quiz,
    *,
    position: int = 0,
    question_type: QuestionType = QuestionType.MCQ,
    **kwargs,  # type: ignore[no-untyped-def]
) -> QuestionFamily:
    family = QuestionFamily(
        quiz_id=quiz.id,
        original_question="What is the time complexity of binary search?",
        question_type=question_type,
        position=position,
        **kwargs,
    )
    db.add(family)
    db.commit()
    return family


def make_variant(
    db: Session,
    family: QuestionFamily,
    *,
    correct_option: int = 1,
    content_hash: str | None = None,
    validation_status: ValidationStatus = ValidationStatus.APPROVED,
    numeric_answer: dict | None = None,
) -> QuestionVariant:
    variant = QuestionVariant(
        family_id=family.id,
        question_text="Binary search on a sorted array runs in?",
        options_json=list(OPTIONS) if numeric_answer is None else None,
        correct_option=correct_option if numeric_answer is None else None,
        numeric_answer_json=numeric_answer,
        content_hash=content_hash or uuid.uuid4().hex,
        validation_status=validation_status,
    )
    db.add(variant)
    db.commit()
    return variant


def make_attempt(
    db: Session,
    quiz: Quiz,
    *,
    email: str = "student@example.edu",
    token: str | None = None,
    duration_seconds: int | None = None,
    started_offset_seconds: int = 0,
    **kwargs,  # type: ignore[no-untyped-def]
) -> tuple[Attempt, str]:
    """Create an attempt and return it with its plaintext token.

    The plaintext is returned rather than stored, mirroring production: the
    student sees it once and the database only ever holds the hash.
    """
    plaintext = token or generate_attempt_token()
    started = utcnow() + timedelta(seconds=started_offset_seconds)
    attempt = Attempt(
        quiz_id=quiz.id,
        student_name="Student One",
        student_email=email.lower(),
        attempt_token_hash=hash_attempt_token(plaintext),
        started_at=started,
        expires_at=started + timedelta(seconds=duration_seconds or quiz.duration_seconds),
        seed=generate_attempt_seed(),
        **kwargs,
    )
    db.add(attempt)
    db.commit()
    return attempt, plaintext


def make_paper(
    db: Session,
    attempt: Attempt,
    variants: list[QuestionVariant],
    *,
    options_order: list[int] | None = None,
) -> list[AttemptQuestion]:
    """Attach a frozen paper to an attempt."""
    questions = []
    for position, variant in enumerate(variants):
        question = AttemptQuestion(
            attempt_id=attempt.id,
            variant_id=variant.id,
            position=position,
            options_order_json=list(options_order) if options_order else None,
        )
        db.add(question)
        questions.append(question)
    db.commit()
    return questions


def answer(db: Session, attempt_question: AttemptQuestion, payload: dict) -> Answer:
    row = Answer(
        attempt_question_id=attempt_question.id,
        selected_answer=payload,
        answered_at=utcnow(),
    )
    db.add(row)
    db.commit()
    return row


def full_attempt(
    db: Session,
    *,
    email: str = "student@example.edu",
    question_count: int = 2,
    duration_seconds: int | None = None,
    started_offset_seconds: int = 0,
) -> tuple[Attempt, str, list[AttemptQuestion]]:
    """An attempt with a real paper, ready to grade."""
    owner = make_user(db, email=f"owner-{uuid.uuid4().hex[:8]}@example.edu")
    quiz = make_quiz(db, owner, question_count=question_count)
    variants = []
    for position in range(question_count):
        family = make_family(db, quiz, position=position)
        variants.append(make_variant(db, family))
    attempt, token = make_attempt(
        db,
        quiz,
        email=email,
        duration_seconds=duration_seconds,
        started_offset_seconds=started_offset_seconds,
    )
    questions = make_paper(db, attempt, variants)
    db.refresh(attempt)
    return attempt, token, questions
