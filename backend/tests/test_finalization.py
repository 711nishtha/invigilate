"""Grading and finalization, including the three race outcomes.

These run on SQLite and prove the *state machine*: whatever order the three
finalization triggers arrive in, an attempt ends up terminal exactly once with
one score. They do not prove the *locking* that makes the race safe under true
concurrency — that needs PostgreSQL and lives in
tests/integration/test_postgres_concurrency.py.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.db.models import AttemptStatus, IntegrityStatus, QuestionType
from app.services.attempt_finalization import finalize_attempt, finalize_if_expired
from app.services.grading import canonical_index, grade_answer, grade_attempt
from tests.factories import (
    answer,
    full_attempt,
    make_attempt,
    make_family,
    make_paper,
    make_quiz,
    make_user,
    make_variant,
)


class TestOptionPermutation:
    def test_displayed_index_maps_through_the_permutation(self) -> None:
        # Screen position 0 shows canonical option 2.
        assert canonical_index(0, [2, 0, 3, 1]) == 2
        assert canonical_index(3, [2, 0, 3, 1]) == 1

    def test_identity_when_no_permutation_was_stored(self) -> None:
        assert canonical_index(2, None) == 2

    def test_out_of_range_index_is_rejected(self) -> None:
        assert canonical_index(9, [2, 0, 3, 1]) is None
        assert canonical_index(-1, [2, 0, 3, 1]) is None

    def test_booleans_are_not_accepted_as_indices(self) -> None:
        """True == 1 in Python; a JSON boolean must not grade as option 1."""
        assert canonical_index(True, [2, 0, 3, 1]) is None  # type: ignore[arg-type]


class TestGrading:
    def test_correct_choice_scores(self, db: Session) -> None:
        attempt, _, questions = full_attempt(db, question_count=1)
        # Canonical correct option is 1; permutation puts it at screen slot 3.
        questions[0].options_order_json = [0, 2, 3, 1]
        db.commit()
        answer(db, questions[0], {"displayed_index": 3})
        db.refresh(questions[0])
        assert grade_answer(questions[0]) is True

    def test_incorrect_choice_does_not_score(self, db: Session) -> None:
        attempt, _, questions = full_attempt(db, question_count=1)
        questions[0].options_order_json = [0, 2, 3, 1]
        db.commit()
        answer(db, questions[0], {"displayed_index": 0})
        db.refresh(questions[0])
        assert grade_answer(questions[0]) is False

    def test_unanswered_question_is_incorrect_not_an_error(self, db: Session) -> None:
        _, _, questions = full_attempt(db, question_count=1)
        assert grade_answer(questions[0]) is False

    def test_client_cannot_assert_its_own_correctness(self, db: Session) -> None:
        """A payload claiming is_correct is ignored; only the key decides."""
        _, _, questions = full_attempt(db, question_count=1)
        questions[0].options_order_json = [0, 2, 3, 1]
        db.commit()
        answer(db, questions[0], {"displayed_index": 0, "is_correct": True, "score": 100})
        db.refresh(questions[0])
        assert grade_answer(questions[0]) is False

    def test_numeric_answer_within_tolerance(self, db: Session) -> None:
        owner = make_user(db)
        quiz = make_quiz(db, owner, question_count=1)
        family = make_family(db, quiz, question_type=QuestionType.NUMERICAL)
        variant = make_variant(db, family, numeric_answer={"value": 83.33, "tolerance": 0.01, "unit": "%"})
        attempt, _ = make_attempt(db, quiz)
        questions = make_paper(db, attempt, [variant])
        answer(db, questions[0], {"value": 83.34})
        db.refresh(questions[0])
        assert grade_answer(questions[0]) is True

    def test_numeric_answer_outside_tolerance(self, db: Session) -> None:
        owner = make_user(db)
        quiz = make_quiz(db, owner, question_count=1)
        family = make_family(db, quiz, question_type=QuestionType.NUMERICAL)
        variant = make_variant(db, family, numeric_answer={"value": 83.33, "tolerance": 0.01})
        attempt, _ = make_attempt(db, quiz)
        questions = make_paper(db, attempt, [variant])
        answer(db, questions[0], {"value": 90})
        db.refresh(questions[0])
        assert grade_answer(questions[0]) is False

    def test_non_numeric_answer_to_a_numeric_question_is_wrong_not_fatal(self, db: Session) -> None:
        owner = make_user(db)
        quiz = make_quiz(db, owner, question_count=1)
        family = make_family(db, quiz, question_type=QuestionType.NUMERICAL)
        variant = make_variant(db, family, numeric_answer={"value": 1.0, "tolerance": 0.0})
        attempt, _ = make_attempt(db, quiz)
        questions = make_paper(db, attempt, [variant])
        answer(db, questions[0], {"value": "drop table students"})
        db.refresh(questions[0])
        assert grade_answer(questions[0]) is False

    def test_percentage_is_computed_from_the_paper_length(self, db: Session) -> None:
        _, _, questions = full_attempt(db, question_count=4)
        for question in questions[:3]:
            question.options_order_json = None
            answer(db, question, {"displayed_index": 1})
        db.commit()
        for question in questions:
            db.refresh(question)
        graded = grade_attempt(questions)
        assert graded.score == Decimal("3.00")
        assert graded.max_score == Decimal("4.00")
        assert graded.percentage == Decimal("75.00")

    def test_is_correct_is_written_back_only_at_grading(self, db: Session) -> None:
        _, _, questions = full_attempt(db, question_count=1)
        questions[0].options_order_json = None
        db.commit()
        row = answer(db, questions[0], {"displayed_index": 1})
        assert row.is_correct is None, "correctness must not exist during the exam"
        db.refresh(questions[0])
        grade_attempt(questions)
        assert questions[0].answer.is_correct is True


class TestIdempotentFinalization:
    def test_a_second_submit_returns_the_first_result(self, db: Session) -> None:
        attempt, _, questions = full_attempt(db, question_count=2)
        questions[0].options_order_json = None
        db.commit()
        answer(db, questions[0], {"displayed_index": 1})
        db.refresh(attempt)

        first = finalize_attempt(db, attempt, reason="MANUAL")
        assert first.already_final is False
        first_score, first_time = attempt.score, attempt.submitted_at

        second = finalize_attempt(db, attempt, reason="MANUAL")
        assert second.already_final is True
        assert attempt.score == first_score
        assert attempt.submitted_at == first_time

    def test_only_the_first_finalization_should_send_results(self, db: Session) -> None:
        attempt, _, _ = full_attempt(db)
        assert finalize_attempt(db, attempt, reason="MANUAL").should_send_results is True
        assert finalize_attempt(db, attempt, reason="MANUAL").should_send_results is False

    def test_an_unknown_reason_is_rejected(self, db: Session) -> None:
        attempt, _, _ = full_attempt(db)
        with pytest.raises(ValueError):
            finalize_attempt(db, attempt, reason="BECAUSE_I_SAID_SO")


class TestRaceOutcomes:
    """The three orderings the review calls for, at state-machine level."""

    def test_two_simultaneous_submissions_finalize_once(self, db: Session) -> None:
        attempt, _, questions = full_attempt(db, question_count=2)
        questions[0].options_order_json = None
        db.commit()
        answer(db, questions[0], {"displayed_index": 1})
        db.refresh(attempt)

        results = [finalize_attempt(db, attempt, reason="MANUAL") for _ in range(2)]

        assert [r.already_final for r in results] == [False, True]
        assert attempt.status is AttemptStatus.SUBMITTED
        assert attempt.score == Decimal("1.00")

    def test_submission_racing_auto_submission_keeps_the_first_outcome(self, db: Session) -> None:
        """Integrity auto-submit lands first; the student's submit does not overwrite it."""
        attempt, _, _ = full_attempt(db)

        auto = finalize_attempt(db, attempt, reason="AUTO_INTEGRITY")
        assert auto.already_final is False
        assert attempt.status is AttemptStatus.AUTO_SUBMITTED
        assert attempt.integrity_status is IntegrityStatus.FLAGGED

        manual = finalize_attempt(db, attempt, reason="MANUAL")
        assert manual.already_final is True
        assert attempt.status is AttemptStatus.AUTO_SUBMITTED, "a later submit must not launder a flag"
        assert attempt.integrity_status is IntegrityStatus.FLAGGED

    def test_auto_submission_arriving_after_a_manual_submit_changes_nothing(self, db: Session) -> None:
        attempt, _, _ = full_attempt(db)
        finalize_attempt(db, attempt, reason="MANUAL")
        result = finalize_attempt(db, attempt, reason="AUTO_INTEGRITY")
        assert result.already_final is True
        assert attempt.status is AttemptStatus.SUBMITTED

    def test_submission_racing_time_expiration(self, db: Session) -> None:
        """Expiry wins when it fires first, and the late submit is a no-op."""
        attempt, _, _ = full_attempt(db, duration_seconds=60, started_offset_seconds=-3600)

        expired = finalize_if_expired(db, attempt)
        assert expired is not None and expired.already_final is False
        assert attempt.status is AttemptStatus.TIME_EXPIRED
        stamped = attempt.submitted_at

        late = finalize_attempt(db, attempt, reason="MANUAL")
        assert late.already_final is True
        assert attempt.status is AttemptStatus.TIME_EXPIRED
        assert attempt.submitted_at == stamped

    def test_expiry_check_is_a_no_op_on_a_live_attempt(self, db: Session) -> None:
        attempt, _, _ = full_attempt(db)
        assert finalize_if_expired(db, attempt) is None
        assert attempt.status is AttemptStatus.IN_PROGRESS

    def test_expiry_check_is_a_no_op_on_an_already_submitted_attempt(self, db: Session) -> None:
        attempt, _, _ = full_attempt(db, duration_seconds=60, started_offset_seconds=-3600)
        finalize_attempt(db, attempt, reason="MANUAL")
        assert finalize_if_expired(db, attempt) is None
        assert attempt.status is AttemptStatus.SUBMITTED

    def test_an_expired_attempt_still_grades_the_answers_it_has(self, db: Session) -> None:
        attempt, _, questions = full_attempt(
            db, question_count=2, duration_seconds=60, started_offset_seconds=-3600
        )
        questions[0].options_order_json = None
        db.commit()
        answer(db, questions[0], {"displayed_index": 1})
        db.refresh(attempt)

        finalize_if_expired(db, attempt)
        assert attempt.status is AttemptStatus.TIME_EXPIRED
        assert attempt.score == Decimal("1.00")
        assert attempt.max_score == Decimal("2.00")


class TestNumericToleranceEdges:
    """Regression cover for the float-boundary bug found in review.

    In binary floating point abs(83.34 - 83.33) > 0.01, which marked an answer
    exactly at the tolerance edge as wrong. Grading compares in Decimal.
    """

    @pytest.mark.parametrize(
        ("expected", "tolerance", "submitted", "correct"),
        [
            (83.33, 0.01, 83.34, True),  # exactly at the edge
            (83.33, 0.01, 83.32, True),  # edge, other side
            (83.33, 0.01, 83.35, False),  # just outside
            (0.1, 0.1, 0.2, True),
            (100, 0, 100, True),  # zero tolerance, exact match
            (100, 0, 100.01, False),
            (2.675, 0.005, 2.68, True),
        ],
    )
    def test_tolerance_boundaries(
        self, db: Session, expected: float, tolerance: float, submitted: float, correct: bool
    ) -> None:
        owner = make_user(db)
        quiz = make_quiz(db, owner, question_count=1)
        family = make_family(db, quiz, question_type=QuestionType.NUMERICAL)
        variant = make_variant(
            db, family, numeric_answer={"value": expected, "tolerance": tolerance}
        )
        attempt, _ = make_attempt(db, quiz)
        questions = make_paper(db, attempt, [variant])
        answer(db, questions[0], {"value": submitted})
        db.refresh(questions[0])
        assert grade_answer(questions[0]) is correct

    def test_a_string_number_is_accepted(self, db: Session) -> None:
        """Some clients send numbers as strings; that is a format, not a wrong answer."""
        owner = make_user(db)
        quiz = make_quiz(db, owner, question_count=1)
        family = make_family(db, quiz, question_type=QuestionType.NUMERICAL)
        variant = make_variant(db, family, numeric_answer={"value": 42, "tolerance": 0})
        attempt, _ = make_attempt(db, quiz)
        questions = make_paper(db, attempt, [variant])
        answer(db, questions[0], {"value": "42"})
        db.refresh(questions[0])
        assert grade_answer(questions[0]) is True

    @pytest.mark.parametrize("hostile", ["NaN", "Infinity", "-Infinity"])
    def test_special_float_literals_do_not_grade_as_correct(self, db: Session, hostile: str) -> None:
        """NaN and Infinity parse as Decimal; neither may score a mark."""
        owner = make_user(db)
        quiz = make_quiz(db, owner, question_count=1)
        family = make_family(db, quiz, question_type=QuestionType.NUMERICAL)
        variant = make_variant(db, family, numeric_answer={"value": 42, "tolerance": 1e9})
        attempt, _ = make_attempt(db, quiz)
        questions = make_paper(db, attempt, [variant])
        answer(db, questions[0], {"value": hostile})
        db.refresh(questions[0])
        assert grade_answer(questions[0]) is False
