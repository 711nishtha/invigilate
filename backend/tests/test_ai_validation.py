"""Validation of provider output, and duplicate detection.

The principle under test: AI generates language, the backend decides what is
acceptable. Malformed output must be rejected with a reason, never repaired
into something plausible and never silently accepted.
"""

from __future__ import annotations

import pytest

from app.ai.base import RawVariant
from app.ai.dedupe import content_hash, find_near_duplicate, normalize_text, similarity
from app.ai.validation import RejectionReason, validate_batch, validate_variant
from app.db.models import QuestionType


def candidate(**overrides) -> RawVariant:  # type: ignore[no-untyped-def]
    data = {
        "question_text": "What is the time complexity of binary search on a sorted array?",
        "options": ["O(1)", "O(log n)", "O(n)", "O(n log n)"],
        "correct_option": 1,
        "explanation": "The search space halves each step, giving logarithmic time.",
        "topic": "Algorithms",
        "difficulty": "medium",
    }
    data.update(overrides)
    return RawVariant(**data, raw=dict(data))


def reasons(failures) -> set[RejectionReason]:  # type: ignore[no-untyped-def]
    return {failure.reason for failure in failures}


class TestAcceptance:
    def test_a_well_formed_variant_is_accepted(self) -> None:
        validated, failures = validate_variant(candidate(), question_type=QuestionType.MCQ)
        assert failures == []
        assert validated is not None
        assert validated.correct_option == 1
        assert validated.difficulty == "medium"
        assert len(validated.content_hash) == 64

    def test_whitespace_is_normalized(self) -> None:
        validated, _ = validate_variant(
            candidate(question_text="  What is the complexity of binary search?  "),
            question_type=QuestionType.MCQ,
        )
        assert validated is not None
        assert not validated.question_text.startswith(" ")

    def test_difficulty_is_lowercased(self) -> None:
        validated, _ = validate_variant(candidate(difficulty="MEDIUM"), question_type=QuestionType.MCQ)
        assert validated is not None and validated.difficulty == "medium"

    def test_family_metadata_fills_a_missing_topic(self) -> None:
        validated, failures = validate_variant(
            candidate(topic=None), question_type=QuestionType.MCQ, fallback_topic="Algorithms"
        )
        assert failures == []
        assert validated is not None and validated.topic == "Algorithms"


class TestMalformedOutput:
    def test_missing_question_is_rejected(self) -> None:
        _, failures = validate_variant(candidate(question_text=""), question_type=QuestionType.MCQ)
        assert RejectionReason.EMPTY_QUESTION in reasons(failures)

    def test_a_too_short_question_is_rejected(self) -> None:
        _, failures = validate_variant(candidate(question_text="Why?"), question_type=QuestionType.MCQ)
        assert RejectionReason.EMPTY_QUESTION in reasons(failures)

    def test_options_that_are_not_a_list_are_rejected(self) -> None:
        _, failures = validate_variant(
            candidate(options="O(1), O(log n)"), question_type=QuestionType.MCQ
        )
        assert RejectionReason.OPTIONS_NOT_A_LIST in reasons(failures)

    def test_wrong_option_count_is_rejected(self) -> None:
        _, failures = validate_variant(
            candidate(options=["O(1)", "O(log n)", "O(n)"]), question_type=QuestionType.MCQ
        )
        assert RejectionReason.WRONG_OPTION_COUNT in reasons(failures)

    def test_duplicate_options_are_rejected(self) -> None:
        """Two identical options mean more than one answer is correct."""
        _, failures = validate_variant(
            candidate(options=["O(log n)", "O(log n)", "O(n)", "O(1)"]),
            question_type=QuestionType.MCQ,
        )
        assert RejectionReason.DUPLICATE_OPTIONS in reasons(failures)

    def test_options_differing_only_by_case_are_duplicates(self) -> None:
        _, failures = validate_variant(
            candidate(options=["O(log n)", "o(LOG N)", "O(n)", "O(1)"]),
            question_type=QuestionType.MCQ,
        )
        assert RejectionReason.DUPLICATE_OPTIONS in reasons(failures)

    def test_blank_option_is_rejected(self) -> None:
        _, failures = validate_variant(
            candidate(options=["O(1)", "   ", "O(n)", "O(n log n)"]),
            question_type=QuestionType.MCQ,
        )
        assert RejectionReason.EMPTY_OPTION in reasons(failures)

    def test_all_of_the_above_is_rejected(self) -> None:
        """It becomes meaningless once options are shuffled per student."""
        _, failures = validate_variant(
            candidate(options=["O(1)", "O(log n)", "O(n)", "All of the above"]),
            question_type=QuestionType.MCQ,
        )
        assert RejectionReason.CATCH_ALL_OPTION in reasons(failures)

    def test_missing_correct_option_is_rejected(self) -> None:
        _, failures = validate_variant(candidate(correct_option=None), question_type=QuestionType.MCQ)
        assert RejectionReason.CORRECT_OPTION_MISSING in reasons(failures)

    def test_out_of_range_correct_option_is_rejected(self) -> None:
        _, failures = validate_variant(candidate(correct_option=9), question_type=QuestionType.MCQ)
        assert RejectionReason.CORRECT_OPTION_OUT_OF_RANGE in reasons(failures)

    def test_negative_correct_option_is_rejected(self) -> None:
        _, failures = validate_variant(candidate(correct_option=-1), question_type=QuestionType.MCQ)
        assert RejectionReason.CORRECT_OPTION_OUT_OF_RANGE in reasons(failures)

    def test_a_string_correct_option_is_rejected(self) -> None:
        _, failures = validate_variant(candidate(correct_option="B"), question_type=QuestionType.MCQ)
        assert RejectionReason.CORRECT_OPTION_NOT_AN_INDEX in reasons(failures)

    def test_a_boolean_correct_option_is_rejected(self) -> None:
        """True == 1 in Python; it must not silently select option 1."""
        _, failures = validate_variant(candidate(correct_option=True), question_type=QuestionType.MCQ)
        assert RejectionReason.CORRECT_OPTION_NOT_AN_INDEX in reasons(failures)

    def test_multiple_correct_answers_are_rejected(self) -> None:
        _, failures = validate_variant(candidate(correct_option=[1, 2]), question_type=QuestionType.MCQ)
        assert RejectionReason.MULTIPLE_CORRECT_ANSWERS in reasons(failures)

    def test_a_parallel_correct_options_key_is_rejected(self) -> None:
        raw = candidate()
        entry = dict(raw.raw)
        entry["correct_options"] = [1, 3]
        _, failures = validate_variant(
            RawVariant(**{k: entry.get(k) for k in
                          ("question_text", "options", "correct_option", "explanation", "topic", "difficulty")},
                       raw=entry),
            question_type=QuestionType.MCQ,
        )
        assert RejectionReason.MULTIPLE_CORRECT_ANSWERS in reasons(failures)

    def test_missing_explanation_is_rejected(self) -> None:
        _, failures = validate_variant(candidate(explanation=None), question_type=QuestionType.MCQ)
        assert RejectionReason.MISSING_EXPLANATION in reasons(failures)

    def test_missing_topic_is_rejected_without_a_fallback(self) -> None:
        _, failures = validate_variant(candidate(topic=None), question_type=QuestionType.MCQ)
        assert RejectionReason.MISSING_TOPIC in reasons(failures)

    def test_unsupported_difficulty_is_rejected(self) -> None:
        _, failures = validate_variant(
            candidate(difficulty="impossible"), question_type=QuestionType.MCQ
        )
        assert RejectionReason.UNSUPPORTED_DIFFICULTY in reasons(failures)

    def test_a_non_object_candidate_is_rejected(self) -> None:
        _, failures = validate_variant(RawVariant(raw={"invalid": "a bare string"}),
                                       question_type=QuestionType.MCQ)
        assert RejectionReason.NOT_AN_OBJECT in reasons(failures)

    def test_short_answer_is_not_generated(self) -> None:
        _, failures = validate_variant(candidate(), question_type=QuestionType.SHORT_ANSWER)
        assert RejectionReason.UNSUPPORTED_QUESTION_TYPE in reasons(failures)

    def test_nothing_malformed_is_ever_silently_accepted(self) -> None:
        """Every malformed shape yields no variant at all, not a repaired one."""
        for bad in [
            candidate(question_text=""),
            candidate(options=[]),
            candidate(correct_option=None),
            candidate(explanation=""),
            candidate(difficulty="???"),
        ]:
            validated, failures = validate_variant(bad, question_type=QuestionType.MCQ)
            assert validated is None and failures


class TestTrueFalse:
    def test_valid_true_false_is_accepted(self) -> None:
        validated, failures = validate_variant(
            candidate(options=["True", "False"], correct_option=0),
            question_type=QuestionType.TRUE_FALSE,
        )
        assert failures == []
        assert validated is not None and validated.options == ["True", "False"]

    def test_wrong_true_false_options_are_rejected(self) -> None:
        _, failures = validate_variant(
            candidate(options=["Yes", "No"], correct_option=0),
            question_type=QuestionType.TRUE_FALSE,
        )
        assert RejectionReason.TRUE_FALSE_OPTIONS_INVALID in reasons(failures)


class TestContentHashing:
    def test_the_same_question_hashes_the_same(self) -> None:
        options = ["a", "b", "c", "d"]
        assert content_hash("What is 2+2?", options) == content_hash("What is 2+2?", options)

    def test_option_order_does_not_change_the_hash(self) -> None:
        """Stored order carries no meaning: each attempt permutes it anyway."""
        assert content_hash("Q", ["a", "b", "c"]) == content_hash("Q", ["c", "a", "b"])

    def test_punctuation_and_case_do_not_change_the_hash(self) -> None:
        assert content_hash("What is the complexity?", None) == content_hash(
            "what is the complexity", None
        )

    def test_different_questions_hash_differently(self) -> None:
        assert content_hash("Question one", None) != content_hash("Question two", None)

    def test_changing_an_option_changes_the_hash(self) -> None:
        assert content_hash("Q", ["a", "b"]) != content_hash("Q", ["a", "c"])

    def test_unicode_lookalikes_normalize_together(self) -> None:
        assert normalize_text("ﬁle") == normalize_text("file")


class TestNearDuplicateDetection:
    def test_identical_text_is_a_near_duplicate(self) -> None:
        found = find_near_duplicate("What is binary search?", [("v1", "What is binary search?")])
        assert found is not None and found[0] == "v1"

    def test_a_reworded_copy_is_caught(self) -> None:
        assert similarity(
            "What is the time complexity of binary search?",
            "What is the time complexity of binary search",
        ) > 0.9

    def test_a_genuinely_different_question_is_not_a_duplicate(self) -> None:
        found = find_near_duplicate(
            "Explain how a hash table resolves collisions using chaining",
            [("v1", "What is the time complexity of binary search?")],
        )
        assert found is None


class TestBatchValidation:
    def test_duplicates_within_one_batch_are_caught(self) -> None:
        result = validate_batch(
            [candidate(), candidate()],
            question_type=QuestionType.MCQ,
            existing=[],
            existing_hashes=set(),
        )
        assert len(result.accepted) == 1
        assert RejectionReason.DUPLICATE_IN_BATCH.value in str(result.rejected[0].failures[0].reason.value)

    def test_duplicates_of_stored_variants_are_caught(self) -> None:
        existing_hash = content_hash(
            candidate().question_text, candidate().options  # type: ignore[arg-type]
        )
        result = validate_batch(
            [candidate()],
            question_type=QuestionType.MCQ,
            existing=[],
            existing_hashes={existing_hash},
        )
        assert result.accepted == []
        assert result.rejected[0].failures[0].reason is RejectionReason.DUPLICATE_OF_EXISTING

    def test_a_partly_valid_batch_keeps_the_good_ones(self) -> None:
        """Some candidates failing is a normal outcome, not a batch failure."""
        result = validate_batch(
            [
                candidate(question_text="Valid question about binary search complexity?"),
                candidate(question_text="", options=["a", "b", "c", "d"]),
                candidate(question_text="Another valid question about hash table collisions?"),
            ],
            question_type=QuestionType.MCQ,
            existing=[],
            existing_hashes=set(),
        )
        assert len(result.accepted) == 2
        assert len(result.rejected) == 1

    def test_every_rejection_carries_a_reason(self) -> None:
        result = validate_batch(
            [candidate(correct_option=None), candidate(options=[])],
            question_type=QuestionType.MCQ,
            existing=[],
            existing_hashes=set(),
        )
        assert len(result.rejected) == 2
        for verdict in result.rejected:
            assert verdict.failures
            for failure in verdict.failures:
                assert failure.to_json()["reason"]

    # "...on a sorted array?" vs "...on a sorted list?" scores 0.9167, which
    # sits just below the 0.92 default: one word differs, so it is a different
    # question and the default correctly keeps it.
    @pytest.mark.parametrize(
        ("threshold", "expect_rejected"),
        [(0.50, True), (0.90, True), (0.92, False), (0.99, False)],
    )
    def test_the_similarity_threshold_is_configurable(
        self, threshold: float, expect_rejected: bool
    ) -> None:
        result = validate_batch(
            [candidate()],
            question_type=QuestionType.MCQ,
            existing=[("v1", "What is the time complexity of binary search on a sorted list?")],
            existing_hashes=set(),
            similarity_threshold=threshold,
        )
        assert (len(result.accepted) == 0) is expect_rejected

    def test_an_exact_reword_is_rejected_at_the_default_threshold(self) -> None:
        """Dropping only punctuation scores 1.0 and must never slip through."""
        result = validate_batch(
            [candidate()],
            question_type=QuestionType.MCQ,
            existing=[
                ("v1", "What is the time complexity of binary search on a sorted array")
            ],
            existing_hashes=set(),
        )
        assert result.accepted == []
        assert result.rejected[0].failures[0].reason is RejectionReason.DUPLICATE_OF_EXISTING
