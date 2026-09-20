"""Schema-level guarantees.

Several security properties are enforced by the database rather than by code,
because a constraint holds even when two requests race. These tests check that
those constraints exist and bite.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.clock import expiry_from, utcnow
from app.core.security import generate_attempt_seed, hash_attempt_token
from app.db.models import (
    Answer,
    Attempt,
    AttemptQuestion,
    AttemptStatus,
    IntegrityStatus,
    QuestionFamily,
    QuestionType,
    QuestionVariant,
    Quiz,
    QuizStatus,
    User,
    UserRole,
    ValidationStatus,
)


def make_user(db: Session, email: str = "teacher@example.edu") -> User:
    user = User(
        supabase_user_id=str(uuid.uuid4()), name="Dr Rao", email=email, role=UserRole.TEACHER
    )
    db.add(user)
    db.commit()
    return user


def make_quiz(db: Session, owner: User, **kwargs: object) -> Quiz:
    quiz = Quiz(
        owner_id=owner.id,
        title="Data Structures Midterm",
        duration_seconds=1200,
        question_count=10,
        **kwargs,  # type: ignore[arg-type]
    )
    db.add(quiz)
    db.commit()
    return quiz


def make_variant(db: Session, quiz: Quiz, content_hash: str = "hash-1") -> QuestionVariant:
    family = QuestionFamily(
        quiz_id=quiz.id,
        original_question="What is the time complexity of binary search?",
        question_type=QuestionType.MCQ,
        position=0,
    )
    db.add(family)
    db.flush()
    variant = QuestionVariant(
        family_id=family.id,
        question_text="Binary search on a sorted array runs in?",
        options_json=["O(1)", "O(log n)", "O(n)", "O(n log n)"],
        correct_option=1,
        content_hash=content_hash,
        validation_status=ValidationStatus.APPROVED,
    )
    db.add(variant)
    db.commit()
    return variant


def make_attempt(db: Session, quiz: Quiz, email: str = "student@example.edu") -> Attempt:
    started = utcnow()
    attempt = Attempt(
        quiz_id=quiz.id,
        student_name="Student One",
        student_email=email,
        attempt_token_hash=hash_attempt_token("token-" + email),
        started_at=started,
        expires_at=expiry_from(started, quiz.duration_seconds),
        seed=generate_attempt_seed(),
    )
    db.add(attempt)
    db.commit()
    return attempt


class TestDefaults:
    def test_quiz_starts_as_an_unpublished_draft(self, db: Session) -> None:
        quiz = make_quiz(db, make_user(db))
        assert quiz.status == QuizStatus.DRAFT
        assert quiz.public_code is None
        assert quiz.is_open_for_attempts is False

    def test_variant_starts_pending(self, db: Session) -> None:
        quiz = make_quiz(db, make_user(db))
        family = QuestionFamily(
            quiz_id=quiz.id, original_question="Q", question_type=QuestionType.MCQ, position=0
        )
        db.add(family)
        db.flush()
        variant = QuestionVariant(
            family_id=family.id, question_text="V", content_hash="h", correct_option=0
        )
        db.add(variant)
        db.commit()
        assert variant.validation_status == ValidationStatus.PENDING
        assert variant.is_approved is False

    def test_attempt_starts_in_progress_and_clean(self, db: Session) -> None:
        attempt = make_attempt(db, make_quiz(db, make_user(db)))
        assert attempt.status == AttemptStatus.IN_PROGRESS
        assert attempt.integrity_status == IntegrityStatus.CLEAN
        assert attempt.integrity_violation_count == 0
        assert attempt.score is None
        assert attempt.is_terminal is False


class TestTerminalStates:
    @pytest.mark.parametrize(
        "status",
        [AttemptStatus.SUBMITTED, AttemptStatus.AUTO_SUBMITTED, AttemptStatus.TIME_EXPIRED],
    )
    def test_finalized_statuses_are_terminal(self, status: AttemptStatus) -> None:
        assert status.is_terminal

    @pytest.mark.parametrize("status", [AttemptStatus.IN_PROGRESS, AttemptStatus.ABANDONED])
    def test_other_statuses_are_not_terminal(self, status: AttemptStatus) -> None:
        assert not status.is_terminal


class TestConstraints:
    def test_one_attempt_per_student_per_quiz(self, db: Session) -> None:
        quiz = make_quiz(db, make_user(db))
        make_attempt(db, quiz, "student@example.edu")
        with pytest.raises(IntegrityError):
            make_attempt(db, quiz, "student@example.edu")

    def test_the_same_student_may_attempt_a_different_quiz(self, db: Session) -> None:
        owner = make_user(db)
        make_attempt(db, make_quiz(db, owner), "student@example.edu")
        make_attempt(db, make_quiz(db, owner), "student@example.edu")

    def test_duplicate_variants_are_refused_by_the_database(self, db: Session) -> None:
        quiz = make_quiz(db, make_user(db))
        variant = make_variant(db, quiz, content_hash="same-hash")
        db.add(
            QuestionVariant(
                family_id=variant.family_id,
                question_text="Reworded but identical",
                content_hash="same-hash",
                correct_option=0,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()

    def test_a_paper_cannot_have_two_questions_at_one_position(self, db: Session) -> None:
        quiz = make_quiz(db, make_user(db))
        variant = make_variant(db, quiz)
        attempt = make_attempt(db, quiz)
        db.add(AttemptQuestion(attempt_id=attempt.id, variant_id=variant.id, position=0))
        db.commit()
        other = make_variant(db, quiz, content_hash="hash-2")
        db.add(AttemptQuestion(attempt_id=attempt.id, variant_id=other.id, position=0))
        with pytest.raises(IntegrityError):
            db.commit()

    def test_a_question_can_only_be_answered_once(self, db: Session) -> None:
        quiz = make_quiz(db, make_user(db))
        variant = make_variant(db, quiz)
        attempt = make_attempt(db, quiz)
        aq = AttemptQuestion(attempt_id=attempt.id, variant_id=variant.id, position=0)
        db.add(aq)
        db.commit()
        db.add(
            Answer(
                attempt_question_id=aq.id,
                selected_answer={"displayed_index": 1},
                answered_at=utcnow(),
            )
        )
        db.commit()
        db.add(
            Answer(
                attempt_question_id=aq.id,
                selected_answer={"displayed_index": 2},
                answered_at=utcnow(),
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()


class TestAnswerKeyStorage:
    def test_correctness_is_absent_until_grading(self, db: Session) -> None:
        """is_correct stays NULL during the exam, so it cannot leak during it."""
        quiz = make_quiz(db, make_user(db))
        variant = make_variant(db, quiz)
        attempt = make_attempt(db, quiz)
        aq = AttemptQuestion(attempt_id=attempt.id, variant_id=variant.id, position=0)
        db.add(aq)
        db.commit()
        answer = Answer(
            attempt_question_id=aq.id, selected_answer={"displayed_index": 1}, answered_at=utcnow()
        )
        db.add(answer)
        db.commit()
        assert answer.is_correct is None

    def test_option_permutation_is_stored_server_side(self, db: Session) -> None:
        quiz = make_quiz(db, make_user(db))
        variant = make_variant(db, quiz)
        attempt = make_attempt(db, quiz)
        aq = AttemptQuestion(
            attempt_id=attempt.id, variant_id=variant.id, position=0, options_order_json=[2, 0, 3, 1]
        )
        db.add(aq)
        db.commit()
        db.refresh(aq)
        assert aq.options_order_json == [2, 0, 3, 1]
