"""Grading.

Pure functions over already-loaded rows: no session, no HTTP, no clock. That
makes the rules below directly testable, which matters because this is the
module that decides marks.

Grading runs exactly once per attempt, at finalization. ``answers.is_correct``
stays NULL for the whole exam so that correctness is not in the database to
leak while the student is still sitting it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from app.db.models import AttemptQuestion, QuestionType


@dataclass(frozen=True)
class GradedAttempt:
    score: Decimal
    max_score: Decimal
    percentage: Decimal


def canonical_index(displayed_index: int, options_order: list[int] | None) -> int | None:
    """Map what the student clicked back to the variant's canonical option.

    ``options_order`` is the per-attempt permutation: position *i* on screen
    shows canonical option ``options_order[i]``. The permutation lives only in
    the database, so a student cannot work out the key by replaying requests
    with different indices.
    """
    if options_order is None:
        return displayed_index
    if not isinstance(displayed_index, int) or isinstance(displayed_index, bool):
        return None
    if displayed_index < 0 or displayed_index >= len(options_order):
        return None
    return options_order[displayed_index]


def _grade_choice(attempt_question: AttemptQuestion, selected: dict[str, Any]) -> bool:
    variant = attempt_question.variant
    if variant.correct_option is None:
        return False
    displayed = selected.get("displayed_index")
    if not isinstance(displayed, int) or isinstance(displayed, bool):
        return False
    chosen = canonical_index(displayed, attempt_question.options_order_json)
    return chosen is not None and chosen == variant.correct_option


def _grade_numeric(attempt_question: AttemptQuestion, selected: dict[str, Any]) -> bool:
    """Compare within the tolerance the backend stored when it computed the answer.

    The expected value is whatever the deterministic generator calculated. A
    model-supplied number never reaches this comparison.

    The comparison is done in ``Decimal`` rather than ``float`` on purpose. In
    binary floating point ``abs(83.34 - 83.33)`` is 0.010000000000005, so a
    student who answers exactly at the edge of a 0.01 tolerance would be marked
    wrong. Converting through ``str`` gives the decimal value the student
    actually typed, and the boundary becomes inclusive as intended.
    """
    answer = attempt_question.variant.numeric_answer_json or {}
    expected = answer.get("value")
    if expected is None:
        return False
    raw = selected.get("value")
    if raw is None or isinstance(raw, bool):
        return False
    try:
        submitted = Decimal(str(raw))
        expected_value = Decimal(str(expected))
        tolerance = abs(Decimal(str(answer.get("tolerance", 0))))
    except (TypeError, ValueError, ArithmeticError):
        # A non-numeric answer to a numeric question is wrong, not an error.
        return False
    if not submitted.is_finite() or not expected_value.is_finite():
        return False
    return abs(submitted - expected_value) <= tolerance


def grade_answer(attempt_question: AttemptQuestion) -> bool:
    """Whether this question was answered correctly.

    Unanswered is incorrect, never an exception: an attempt that a student
    abandoned must still grade cleanly.
    """
    answer = attempt_question.answer
    if answer is None or not isinstance(answer.selected_answer, dict):
        return False

    question_type = attempt_question.variant.family.question_type
    if question_type in (QuestionType.MCQ, QuestionType.TRUE_FALSE):
        return _grade_choice(attempt_question, answer.selected_answer)
    if question_type is QuestionType.NUMERICAL:
        return _grade_numeric(attempt_question, answer.selected_answer)
    # SHORT_ANSWER is not auto-graded in the MVP; it scores zero and is left
    # for the teacher rather than guessed at.
    return False


def grade_attempt(attempt_questions: list[AttemptQuestion]) -> GradedAttempt:
    """Grade every question and write ``is_correct`` back onto the answers.

    One mark per question. Weighting is a later concern; when it arrives it
    belongs here and nowhere else.
    """
    correct = 0
    for attempt_question in attempt_questions:
        is_correct = grade_answer(attempt_question)
        if attempt_question.answer is not None:
            attempt_question.answer.is_correct = is_correct
        if is_correct:
            correct += 1

    max_score = Decimal(len(attempt_questions))
    score = Decimal(correct)
    percentage = (
        (score / max_score * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if max_score > 0
        else Decimal("0.00")
    )
    return GradedAttempt(
        score=score.quantize(Decimal("0.01")),
        max_score=max_score.quantize(Decimal("0.01")),
        percentage=percentage,
    )


__all__ = ["GradedAttempt", "canonical_index", "grade_answer", "grade_attempt"]
