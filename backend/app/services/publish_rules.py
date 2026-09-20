"""Publish eligibility.

A quiz becomes publishable only when its content can actually produce a valid
paper for every student. The checks below are returned as a structured report
rather than a bare boolean, because a teacher blocked from publishing needs to
know exactly what to fix.

Each rule is a named check so the set can grow without the call sites changing,
and so a future generation strategy can add its own rule rather than having to
fit inside a hard-coded assumption.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from sqlalchemy.orm import Session

from app.db.models import QuestionFamily, QuestionType, Quiz


class BlockerCode(str, Enum):
    NO_QUESTION_FAMILIES = "NO_QUESTION_FAMILIES"
    NOT_ENOUGH_FAMILIES = "NOT_ENOUGH_FAMILIES"
    FAMILY_WITHOUT_APPROVED_VARIANT = "FAMILY_WITHOUT_APPROVED_VARIANT"
    VARIANT_MISSING_OPTIONS = "VARIANT_MISSING_OPTIONS"
    VARIANT_MISSING_CORRECT_ANSWER = "VARIANT_MISSING_CORRECT_ANSWER"
    VARIANT_CORRECT_OPTION_OUT_OF_RANGE = "VARIANT_CORRECT_OPTION_OUT_OF_RANGE"
    VARIANT_DUPLICATE_OPTIONS = "VARIANT_DUPLICATE_OPTIONS"
    VARIANT_MISSING_NUMERIC_ANSWER = "VARIANT_MISSING_NUMERIC_ANSWER"
    DUPLICATE_VARIANTS_IN_FAMILY = "DUPLICATE_VARIANTS_IN_FAMILY"
    FAMILY_MISSING_METADATA = "FAMILY_MISSING_METADATA"
    UNSUPPORTED_QUESTION_TYPE = "UNSUPPORTED_QUESTION_TYPE"
    TOPIC_COVERAGE_UNMET = "TOPIC_COVERAGE_UNMET"
    DIFFICULTY_COVERAGE_UNMET = "DIFFICULTY_COVERAGE_UNMET"


@dataclass(frozen=True)
class Blocker:
    code: BlockerCode
    message: str
    family_id: str | None = None
    variant_id: str | None = None

    def to_json(self) -> dict[str, str | None]:
        return {
            "code": self.code.value,
            "message": self.message,
            "family_id": self.family_id,
            "variant_id": self.variant_id,
        }


@dataclass
class PublishReadiness:
    """Everything the teacher UI needs to explain publishability."""

    publishable: bool
    blockers: list[Blocker] = field(default_factory=list)
    family_count: int = 0
    ready_family_count: int = 0
    assignable_variant_count: int = 0
    question_count: int = 0
    # Distinct papers before option shuffling: the product of each ready
    # family's assignable variant count. Shown to the teacher so that
    # "10 questions x 5 variants" means something concrete. Capped because the
    # true product overflows quickly and the exact figure is not useful past a
    # certain size.
    distinct_paper_estimate: int = 0


# Types the MVP can assemble and grade automatically. SHORT_ANSWER is stored
# and reviewable but cannot be auto-graded, so it cannot yet carry an exam.
SUPPORTED_FOR_PUBLICATION = frozenset(
    {QuestionType.MCQ, QuestionType.TRUE_FALSE, QuestionType.NUMERICAL}
)


def _check_variant(family: QuestionFamily, variant, blockers: list[Blocker]) -> None:  # type: ignore[no-untyped-def]
    """Structural checks on a variant that is about to become assignable."""
    identity = {"family_id": str(family.id), "variant_id": str(variant.id)}

    if family.question_type in (QuestionType.MCQ, QuestionType.TRUE_FALSE):
        options = variant.options_json
        if not options or not isinstance(options, list) or len(options) < 2:
            blockers.append(
                Blocker(
                    BlockerCode.VARIANT_MISSING_OPTIONS,
                    "An approved choice variant must have at least two options.",
                    **identity,
                )
            )
            return
        if len({str(option).strip().casefold() for option in options}) != len(options):
            blockers.append(
                Blocker(
                    BlockerCode.VARIANT_DUPLICATE_OPTIONS,
                    "Options must be distinct, otherwise more than one answer is correct.",
                    **identity,
                )
            )
        if variant.correct_option is None:
            blockers.append(
                Blocker(
                    BlockerCode.VARIANT_MISSING_CORRECT_ANSWER,
                    "An approved choice variant must record exactly one correct option.",
                    **identity,
                )
            )
        elif not 0 <= variant.correct_option < len(options):
            blockers.append(
                Blocker(
                    BlockerCode.VARIANT_CORRECT_OPTION_OUT_OF_RANGE,
                    f"correct_option {variant.correct_option} is outside the {len(options)} options.",
                    **identity,
                )
            )
    elif family.question_type is QuestionType.NUMERICAL:
        answer = variant.numeric_answer_json
        if not isinstance(answer, dict) or answer.get("value") is None:
            blockers.append(
                Blocker(
                    BlockerCode.VARIANT_MISSING_NUMERIC_ANSWER,
                    "An approved numeric variant must have a backend-computed answer.",
                    **identity,
                )
            )


def evaluate_publish_readiness(db: Session, quiz: Quiz) -> PublishReadiness:
    """Report whether ``quiz`` can be published, and why not if it cannot."""
    blockers: list[Blocker] = []
    families = list(quiz.question_families)

    readiness = PublishReadiness(
        publishable=False,
        family_count=len(families),
        question_count=quiz.question_count,
    )

    if not families:
        blockers.append(
            Blocker(
                BlockerCode.NO_QUESTION_FAMILIES,
                "Add at least one question before publishing.",
            )
        )
        readiness.blockers = blockers
        return readiness

    coverage = quiz.coverage_config_json or {}
    ready_families = 0
    assignable_total = 0
    variant_product = 1
    topics_covered: set[str] = set()
    difficulties_covered: set[str] = set()

    for family in families:
        if family.question_type not in SUPPORTED_FOR_PUBLICATION:
            blockers.append(
                Blocker(
                    BlockerCode.UNSUPPORTED_QUESTION_TYPE,
                    f"{family.question_type.value} cannot be auto-graded yet, so it cannot be published.",
                    family_id=str(family.id),
                )
            )
            continue

        if not family.original_question or not family.original_question.strip():
            blockers.append(
                Blocker(
                    BlockerCode.FAMILY_MISSING_METADATA,
                    "The original question is required.",
                    family_id=str(family.id),
                )
            )

        assignable = [v for v in family.variants if v.is_assignable]
        if not assignable:
            blockers.append(
                Blocker(
                    BlockerCode.FAMILY_WITHOUT_APPROVED_VARIANT,
                    "This question has no approved variant, so no student could be given it.",
                    family_id=str(family.id),
                )
            )
            continue

        hashes = [v.content_hash for v in assignable]
        if len(set(hashes)) != len(hashes):
            blockers.append(
                Blocker(
                    BlockerCode.DUPLICATE_VARIANTS_IN_FAMILY,
                    "Two approved variants have identical content.",
                    family_id=str(family.id),
                )
            )

        for variant in assignable:
            _check_variant(family, variant, blockers)

        ready_families += 1
        assignable_total += len(assignable)
        variant_product *= len(assignable)
        if family.topic:
            topics_covered.add(family.topic.strip().casefold())
        if family.difficulty:
            difficulties_covered.add(family.difficulty.strip().casefold())

    if ready_families < quiz.question_count:
        blockers.append(
            Blocker(
                BlockerCode.NOT_ENOUGH_FAMILIES,
                f"This quiz gives each student {quiz.question_count} questions, but only "
                f"{ready_families} question(s) have an approved variant.",
            )
        )

    # Optional coverage requirements, declared per quiz. Absent by default so a
    # future generation strategy is not boxed in by an assumption made here.
    required_topics = {t.strip().casefold() for t in coverage.get("required_topics", []) if t}
    missing_topics = required_topics - topics_covered
    if missing_topics:
        blockers.append(
            Blocker(
                BlockerCode.TOPIC_COVERAGE_UNMET,
                f"No ready question covers: {', '.join(sorted(missing_topics))}.",
            )
        )

    required_difficulties = {
        d.strip().casefold() for d in coverage.get("required_difficulties", []) if d
    }
    missing_difficulties = required_difficulties - difficulties_covered
    if missing_difficulties:
        blockers.append(
            Blocker(
                BlockerCode.DIFFICULTY_COVERAGE_UNMET,
                f"No ready question is at difficulty: {', '.join(sorted(missing_difficulties))}.",
            )
        )

    readiness.blockers = blockers
    readiness.ready_family_count = ready_families
    readiness.assignable_variant_count = assignable_total
    readiness.distinct_paper_estimate = min(variant_product, 10**12) if ready_families else 0
    readiness.publishable = not blockers
    return readiness


__all__ = [
    "Blocker",
    "BlockerCode",
    "PublishReadiness",
    "SUPPORTED_FOR_PUBLICATION",
    "evaluate_publish_readiness",
]
