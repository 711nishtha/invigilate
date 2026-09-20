"""Deterministic validation of provider output.

This is where "AI generates language, the backend decides truth" is actually
enforced. Nothing reaches the database until it has passed every check here,
and a malformed candidate is rejected with a reason rather than repaired into
something that looks plausible.

Validation is pure: it takes untrusted candidates plus the family's existing
variants and returns verdicts. It touches no session and no network, which is
why it can be tested exhaustively.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.ai.base import RawVariant
from app.ai.dedupe import (
    DEFAULT_SIMILARITY_THRESHOLD,
    content_hash,
    find_near_duplicate,
    normalize_text,
)
from app.db.models import QuestionType

MIN_QUESTION_LENGTH = 10
MIN_EXPLANATION_LENGTH = 10
MAX_QUESTION_LENGTH = 4000
MAX_OPTION_LENGTH = 800
VALID_DIFFICULTIES = ("easy", "medium", "hard")


class RejectionReason(str, Enum):
    """Why a candidate was refused. Stored so a teacher can see it."""

    NOT_AN_OBJECT = "NOT_AN_OBJECT"
    EMPTY_QUESTION = "EMPTY_QUESTION"
    QUESTION_TOO_LONG = "QUESTION_TOO_LONG"
    OPTIONS_NOT_A_LIST = "OPTIONS_NOT_A_LIST"
    WRONG_OPTION_COUNT = "WRONG_OPTION_COUNT"
    EMPTY_OPTION = "EMPTY_OPTION"
    OPTION_TOO_LONG = "OPTION_TOO_LONG"
    DUPLICATE_OPTIONS = "DUPLICATE_OPTIONS"
    CATCH_ALL_OPTION = "CATCH_ALL_OPTION"
    CORRECT_OPTION_MISSING = "CORRECT_OPTION_MISSING"
    CORRECT_OPTION_NOT_AN_INDEX = "CORRECT_OPTION_NOT_AN_INDEX"
    CORRECT_OPTION_OUT_OF_RANGE = "CORRECT_OPTION_OUT_OF_RANGE"
    MULTIPLE_CORRECT_ANSWERS = "MULTIPLE_CORRECT_ANSWERS"
    MISSING_TOPIC = "MISSING_TOPIC"
    MISSING_DIFFICULTY = "MISSING_DIFFICULTY"
    UNSUPPORTED_DIFFICULTY = "UNSUPPORTED_DIFFICULTY"
    MISSING_EXPLANATION = "MISSING_EXPLANATION"
    UNSUPPORTED_QUESTION_TYPE = "UNSUPPORTED_QUESTION_TYPE"
    TRUE_FALSE_OPTIONS_INVALID = "TRUE_FALSE_OPTIONS_INVALID"
    DUPLICATE_OF_EXISTING = "DUPLICATE_OF_EXISTING"
    DUPLICATE_IN_BATCH = "DUPLICATE_IN_BATCH"


@dataclass(frozen=True)
class ValidationFailure:
    reason: RejectionReason
    detail: str = ""

    def to_json(self) -> dict[str, str]:
        return {"reason": self.reason.value, "detail": self.detail}


@dataclass
class ValidatedVariant:
    """A candidate that passed every check, normalized for storage."""

    question_text: str
    options: list[str] | None
    correct_option: int | None
    explanation: str
    topic: str
    difficulty: str
    content_hash: str


@dataclass
class VariantVerdict:
    """One candidate's outcome."""

    index: int
    accepted: ValidatedVariant | None = None
    failures: list[ValidationFailure] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.accepted is not None and not self.failures


@dataclass
class BatchValidationResult:
    verdicts: list[VariantVerdict]

    @property
    def accepted(self) -> list[ValidatedVariant]:
        return [v.accepted for v in self.verdicts if v.ok and v.accepted is not None]

    @property
    def rejected(self) -> list[VariantVerdict]:
        return [v for v in self.verdicts if not v.ok]


# Options that make a question untestable under option shuffling: "all of the
# above" is meaningless once the order changes.
_CATCH_ALL = ("all of the above", "none of the above", "both a and b", "all the above")


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _validate_options(
    raw_options: Any, question_type: QuestionType, expected_count: int
) -> tuple[list[str] | None, list[ValidationFailure]]:
    failures: list[ValidationFailure] = []

    if not isinstance(raw_options, list):
        return None, [ValidationFailure(RejectionReason.OPTIONS_NOT_A_LIST, str(type(raw_options)))]

    options: list[str] = []
    for position, option in enumerate(raw_options):
        cleaned = _clean(option)
        if cleaned is None:
            failures.append(ValidationFailure(RejectionReason.EMPTY_OPTION, f"index {position}"))
            continue
        if len(cleaned) > MAX_OPTION_LENGTH:
            failures.append(ValidationFailure(RejectionReason.OPTION_TOO_LONG, f"index {position}"))
            continue
        options.append(cleaned)

    if question_type is QuestionType.TRUE_FALSE:
        if [normalize_text(o) for o in options] != ["true", "false"]:
            failures.append(
                ValidationFailure(
                    RejectionReason.TRUE_FALSE_OPTIONS_INVALID,
                    f"expected ['True', 'False'], got {options}",
                )
            )
    elif len(options) != expected_count:
        failures.append(
            ValidationFailure(
                RejectionReason.WRONG_OPTION_COUNT,
                f"expected {expected_count}, got {len(options)}",
            )
        )

    normalized = [normalize_text(option) for option in options]
    if len(set(normalized)) != len(normalized):
        failures.append(ValidationFailure(RejectionReason.DUPLICATE_OPTIONS, ""))

    for option, norm in zip(options, normalized, strict=True):
        if norm in _CATCH_ALL:
            failures.append(ValidationFailure(RejectionReason.CATCH_ALL_OPTION, option))

    return options, failures


def _validate_correct_option(
    raw: Any, options: list[str] | None, entry: dict[str, Any]
) -> tuple[int | None, list[ValidationFailure]]:
    failures: list[ValidationFailure] = []

    # A model that answers with a list has proposed more than one correct
    # answer, which the format does not allow.
    if isinstance(raw, (list, tuple, set)):
        return None, [
            ValidationFailure(RejectionReason.MULTIPLE_CORRECT_ANSWERS, f"{list(raw)!r}")
        ]
    # Some models add a parallel "correct_options" key; treat that the same way.
    extra = entry.get("correct_options")
    if isinstance(extra, (list, tuple)) and len(extra) > 1:
        return None, [ValidationFailure(RejectionReason.MULTIPLE_CORRECT_ANSWERS, f"{list(extra)!r}")]

    if raw is None:
        return None, [ValidationFailure(RejectionReason.CORRECT_OPTION_MISSING, "")]
    # bool is an int subclass; True must not silently mean option 1.
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None, [
            ValidationFailure(RejectionReason.CORRECT_OPTION_NOT_AN_INDEX, repr(raw))
        ]
    if options is None or raw < 0 or raw >= len(options):
        failures.append(
            ValidationFailure(
                RejectionReason.CORRECT_OPTION_OUT_OF_RANGE,
                f"index {raw}, {len(options or [])} options",
            )
        )
        return None, failures
    return raw, failures


def validate_variant(
    candidate: RawVariant,
    *,
    question_type: QuestionType,
    option_count: int = 4,
    fallback_topic: str | None = None,
    fallback_difficulty: str | None = None,
) -> tuple[ValidatedVariant | None, list[ValidationFailure]]:
    """Check one candidate. Returns the normalized variant, or the failures."""
    if question_type not in (QuestionType.MCQ, QuestionType.TRUE_FALSE):
        # NUMERICAL goes through app.ai.numeric; SHORT_ANSWER is not generated.
        return None, [
            ValidationFailure(RejectionReason.UNSUPPORTED_QUESTION_TYPE, question_type.value)
        ]

    if not isinstance(candidate.raw, dict) or "invalid" in candidate.raw:
        return None, [ValidationFailure(RejectionReason.NOT_AN_OBJECT, "")]

    failures: list[ValidationFailure] = []

    question_text = _clean(candidate.question_text)
    if question_text is None or len(question_text) < MIN_QUESTION_LENGTH:
        failures.append(
            ValidationFailure(RejectionReason.EMPTY_QUESTION, f"{len(question_text or '')} chars")
        )
    elif len(question_text) > MAX_QUESTION_LENGTH:
        failures.append(ValidationFailure(RejectionReason.QUESTION_TOO_LONG, ""))

    expected = 2 if question_type is QuestionType.TRUE_FALSE else option_count
    options, option_failures = _validate_options(candidate.options, question_type, expected)
    failures.extend(option_failures)

    correct_option, correct_failures = _validate_correct_option(
        candidate.correct_option, options, candidate.raw
    )
    failures.extend(correct_failures)

    explanation = _clean(candidate.explanation)
    if explanation is None or len(explanation) < MIN_EXPLANATION_LENGTH:
        failures.append(ValidationFailure(RejectionReason.MISSING_EXPLANATION, ""))

    # The family's own metadata is a legitimate fallback: the teacher already
    # declared the topic, so a model omitting it is untidy rather than fatal.
    topic = _clean(candidate.topic) or _clean(fallback_topic)
    if topic is None:
        failures.append(ValidationFailure(RejectionReason.MISSING_TOPIC, ""))

    difficulty_raw = _clean(candidate.difficulty) or _clean(fallback_difficulty)
    difficulty = None
    if difficulty_raw is None:
        failures.append(ValidationFailure(RejectionReason.MISSING_DIFFICULTY, ""))
    elif difficulty_raw.lower() not in VALID_DIFFICULTIES:
        failures.append(
            ValidationFailure(RejectionReason.UNSUPPORTED_DIFFICULTY, difficulty_raw)
        )
    else:
        difficulty = difficulty_raw.lower()

    if failures:
        return None, failures

    assert question_text is not None and options is not None
    assert correct_option is not None and explanation is not None
    assert topic is not None and difficulty is not None

    return (
        ValidatedVariant(
            question_text=question_text,
            options=options,
            correct_option=correct_option,
            explanation=explanation,
            topic=topic,
            difficulty=difficulty,
            content_hash=content_hash(question_text, options),
        ),
        [],
    )


def validate_batch(
    candidates: list[RawVariant],
    *,
    question_type: QuestionType,
    existing: list[tuple[str, str]],
    existing_hashes: set[str],
    option_count: int = 4,
    fallback_topic: str | None = None,
    fallback_difficulty: str | None = None,
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> BatchValidationResult:
    """Validate a whole batch, including duplicates against each other.

    A batch where some candidates fail is a normal outcome, not an error: the
    caller stores the survivors and reports the rejections.
    """
    verdicts: list[VariantVerdict] = []
    # Grows as the batch is processed, so two identical candidates in one
    # response cannot both be accepted.
    seen_hashes = set(existing_hashes)
    seen_texts = list(existing)

    for index, candidate in enumerate(candidates):
        validated, failures = validate_variant(
            candidate,
            question_type=question_type,
            option_count=option_count,
            fallback_topic=fallback_topic,
            fallback_difficulty=fallback_difficulty,
        )
        if validated is None:
            verdicts.append(VariantVerdict(index=index, failures=failures))
            continue

        if validated.content_hash in seen_hashes:
            in_batch = validated.content_hash not in existing_hashes
            verdicts.append(
                VariantVerdict(
                    index=index,
                    failures=[
                        ValidationFailure(
                            RejectionReason.DUPLICATE_IN_BATCH
                            if in_batch
                            else RejectionReason.DUPLICATE_OF_EXISTING,
                            "identical content hash",
                        )
                    ],
                )
            )
            continue

        near = find_near_duplicate(
            validated.question_text, seen_texts, threshold=similarity_threshold
        )
        if near is not None:
            identifier, score = near
            verdicts.append(
                VariantVerdict(
                    index=index,
                    failures=[
                        ValidationFailure(
                            RejectionReason.DUPLICATE_OF_EXISTING,
                            f"similarity {score:.2f} to {identifier}",
                        )
                    ],
                )
            )
            continue

        seen_hashes.add(validated.content_hash)
        seen_texts.append((f"batch[{index}]", validated.question_text))
        verdicts.append(VariantVerdict(index=index, accepted=validated))

    return BatchValidationResult(verdicts=verdicts)


__all__ = [
    "BatchValidationResult",
    "RejectionReason",
    "ValidatedVariant",
    "ValidationFailure",
    "VariantVerdict",
    "validate_batch",
    "validate_variant",
]
