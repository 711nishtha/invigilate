"""Variant generation, editing and review.

Generation happens while a teacher prepares a quiz — never when a student
starts an attempt. An exam must not depend on a third-party API being up, and
nothing here is reachable from a student route.

The pipeline is: provider -> strict validation -> duplicate detection ->
persist as VALIDATED -> teacher approves. A provider failure is recorded and
returned; it never leaves the family half-written and never blocks a quiz that
is already published.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.ai.base import (
    AIProviderError,
    RawVariantBatch,
    VariantGenerationRequest,
)
from app.ai.dedupe import DEFAULT_SIMILARITY_THRESHOLD, content_hash
from app.ai.factory import get_ai_provider
from app.ai.numeric import generate_numeric_variants
from app.ai.validation import (
    RejectionReason,
    ValidationFailure,
    validate_batch,
)
from app.core.config import settings
from app.core.errors import AppError, Conflict
from app.core.logging import Event, get_logger, log_event
from app.core.security import generate_attempt_seed
from app.db.models import (
    GenerationStatus,
    QuestionFamily,
    QuestionType,
    QuestionVariant,
    User,
    VariantStatus,
)
from app.services.variant_lifecycle import is_in_use, transition

logger = get_logger("securequiz.variants")

MAX_VARIANTS_PER_REQUEST = 20


@dataclass
class RejectedCandidate:
    index: int
    reasons: list[dict[str, str]]


@dataclass
class GenerationOutcome:
    """What a generation run produced. Partial success is a normal result."""

    family_id: str
    requested: int
    accepted: list[QuestionVariant] = field(default_factory=list)
    rejected: list[RejectedCandidate] = field(default_factory=list)
    provider: str = ""
    model: str | None = None
    prompt_version: str | None = None
    # Set when the provider itself failed. The family is untouched in that case.
    error_code: str | None = None
    error_message: str | None = None

    @property
    def accepted_count(self) -> int:
        return len(self.accepted)

    @property
    def failed(self) -> bool:
        return self.error_code is not None


def _existing_for_dedupe(family: QuestionFamily) -> tuple[list[tuple[str, str]], set[str]]:
    """Texts and hashes already in this family, for duplicate detection.

    Rejected variants are included on purpose: regenerating the same rejected
    question wastes the teacher's time, so it is caught as a duplicate.
    """
    texts = [(str(v.id), v.question_text) for v in family.variants]
    hashes = {v.content_hash for v in family.variants}
    return texts, hashes


async def generate_variants(
    db: Session,
    family: QuestionFamily,
    *,
    count: int | None = None,
    difficulty: str | None = None,
) -> GenerationOutcome:
    """Generate, validate and store candidate variants for one family."""
    requested = count or int(
        family.generation_config_json.get("variant_count")
        or settings.default_variants_per_question
    )
    if requested < 1 or requested > MAX_VARIANTS_PER_REQUEST:
        raise AppError(
            f"Request between 1 and {MAX_VARIANTS_PER_REQUEST} variants.",
            code="INVALID_VARIANT_COUNT",
        )

    log_event(
        Event.VARIANT_GENERATION_STARTED,
        family_id=str(family.id),
        quiz_id=str(family.quiz_id),
        requested=requested,
        question_type=family.question_type.value,
    )

    if family.question_type is QuestionType.NUMERICAL:
        return _generate_numeric(db, family, requested)
    if family.question_type is QuestionType.SHORT_ANSWER:
        raise AppError(
            "Short-answer questions are not generated automatically. Add variants by hand.",
            code="GENERATION_NOT_SUPPORTED",
        )

    return await _generate_choice(db, family, requested, difficulty)


async def _generate_choice(
    db: Session, family: QuestionFamily, requested: int, difficulty: str | None
) -> GenerationOutcome:
    provider = get_ai_provider()
    outcome = GenerationOutcome(
        family_id=str(family.id), requested=requested, provider=provider.name
    )

    existing_texts, existing_hashes = _existing_for_dedupe(family)
    request = VariantGenerationRequest(
        original_question=family.original_question,
        question_type=family.question_type,
        count=requested,
        topic=family.topic,
        difficulty=difficulty or family.difficulty,
        learning_objective=family.learning_objective,
        option_count=int(family.generation_config_json.get("option_count", 4)),
        avoid_texts=tuple(text for _, text in existing_texts),
    )

    try:
        batch: RawVariantBatch = await provider.generate_question_variants(request)
    except AIProviderError as exc:
        # The family is untouched. A provider outage is a reportable event, not
        # a corrupted quiz, and an already-published quiz keeps working.
        outcome.error_code = type(exc).__name__
        outcome.error_message = exc.message
        log_event(
            Event.VARIANT_GENERATION_FAILED,
            family_id=str(family.id),
            provider=provider.name,
            error=type(exc).__name__,
            retryable=exc.retryable,
        )
        return outcome

    outcome.model = batch.model
    outcome.prompt_version = batch.prompt_version

    result = validate_batch(
        batch.variants,
        question_type=family.question_type,
        existing=existing_texts,
        existing_hashes=existing_hashes,
        option_count=request.option_count,
        fallback_topic=family.topic,
        fallback_difficulty=request.difficulty,
        similarity_threshold=float(
            family.generation_config_json.get("dedupe_threshold", DEFAULT_SIMILARITY_THRESHOLD)
        ),
    )

    for verdict in result.verdicts:
        if verdict.ok and verdict.accepted is not None:
            validated = verdict.accepted
            variant = QuestionVariant(
                family_id=family.id,
                question_text=validated.question_text,
                options_json=validated.options,
                correct_option=validated.correct_option,
                explanation=validated.explanation,
                generation_model=batch.model,
                generation_prompt_version=batch.prompt_version,
                generation_status=GenerationStatus.AI_GENERATED,
                # Passed deterministic validation, awaiting a human.
                validation_status=VariantStatus.VALIDATED,
                content_hash=validated.content_hash,
            )
            stored = _persist_variant(db, variant)
            if stored is None:
                outcome.rejected.append(
                    RejectedCandidate(
                        index=verdict.index,
                        reasons=[
                            ValidationFailure(
                                RejectionReason.DUPLICATE_OF_EXISTING,
                                "another request stored identical content first",
                            ).to_json()
                        ],
                    )
                )
            else:
                outcome.accepted.append(stored)
        else:
            outcome.rejected.append(
                RejectedCandidate(
                    index=verdict.index,
                    reasons=[failure.to_json() for failure in verdict.failures],
                )
            )

    log_event(
        Event.VARIANT_GENERATION_COMPLETED,
        family_id=str(family.id),
        provider=provider.name,
        model=batch.model,
        requested=requested,
        accepted=outcome.accepted_count,
        rejected=len(outcome.rejected),
    )
    return outcome


def _generate_numeric(
    db: Session, family: QuestionFamily, requested: int
) -> GenerationOutcome:
    """Numeric variants are computed, never generated by a model.

    The stem comes from the teacher's template with seeded parameters
    substituted, and the answer from evaluating the teacher's expression in
    backend code. No model-produced number is ever stored.
    """
    outcome = GenerationOutcome(
        family_id=str(family.id), requested=requested, provider="deterministic"
    )

    _, existing_hashes = _existing_for_dedupe(family)
    seed = int(family.generation_config_json.get("seed") or generate_attempt_seed())

    generated = generate_numeric_variants(
        family.generation_config_json, count=requested, seed=seed
    )

    for index, numeric in enumerate(generated):
        if numeric.content_hash in existing_hashes:
            outcome.rejected.append(
                RejectedCandidate(
                    index=index,
                    reasons=[
                        ValidationFailure(
                            RejectionReason.DUPLICATE_OF_EXISTING,
                            "these parameters already produced a variant",
                        ).to_json()
                    ],
                )
            )
            continue

        variant = QuestionVariant(
            family_id=family.id,
            question_text=numeric.question_text,
            options_json=None,
            correct_option=None,
            numeric_answer_json=numeric.numeric_answer_json(),
            explanation=None,
            generation_model=None,
            generation_prompt_version=None,
            # The distinction that matters: the answer was computed, not written.
            generation_status=GenerationStatus.DETERMINISTIC,
            validation_status=VariantStatus.VALIDATED,
            content_hash=numeric.content_hash,
        )
        stored = _persist_variant(db, variant)
        if stored is not None:
            outcome.accepted.append(stored)
            existing_hashes.add(numeric.content_hash)

    log_event(
        Event.VARIANT_GENERATION_COMPLETED,
        family_id=str(family.id),
        provider="deterministic",
        requested=requested,
        accepted=outcome.accepted_count,
        rejected=len(outcome.rejected),
    )
    return outcome


def _persist_variant(db: Session, variant: QuestionVariant) -> QuestionVariant | None:
    """Insert one variant, treating a hash collision as a duplicate.

    ``UNIQUE (family_id, content_hash)`` is the authority on duplicates, so two
    concurrent generation runs cannot both store the same question. A savepoint
    keeps one rejection from poisoning the rest of the batch.
    """
    savepoint = db.begin_nested()
    try:
        db.add(variant)
        savepoint.commit()
    except IntegrityError:
        savepoint.rollback()
        return None
    db.commit()
    db.refresh(variant)
    return variant


def create_manual_variant(
    db: Session, family: QuestionFamily, data: dict[str, Any]
) -> QuestionVariant:
    """Add a variant a teacher wrote by hand.

    Still validated and still requiring approval: the pipeline does not have a
    trusted path that skips the checks.
    """
    options = data.get("options_json")
    digest = content_hash(data["question_text"], options)

    if db.scalar(
        select(QuestionVariant.id).where(
            QuestionVariant.family_id == family.id, QuestionVariant.content_hash == digest
        )
    ):
        raise Conflict(
            "A variant with identical content already exists in this question.",
            code="DUPLICATE_VARIANT",
        )

    variant = QuestionVariant(
        family_id=family.id,
        question_text=data["question_text"],
        options_json=options,
        correct_option=data.get("correct_option"),
        numeric_answer_json=data.get("numeric_answer_json"),
        explanation=data.get("explanation"),
        generation_status=GenerationStatus.MANUAL,
        validation_status=VariantStatus.VALIDATED,
        content_hash=digest,
    )
    db.add(variant)
    db.commit()
    db.refresh(variant)
    return variant


def edit_variant(
    db: Session, variant: QuestionVariant, changes: dict[str, Any], actor: User
) -> QuestionVariant:
    """Edit a variant, superseding it if it is no longer mutable.

    This is the rule that keeps history honest. A GENERATED or VALIDATED
    variant has never been shown to anyone, so it is edited in place. An
    APPROVED or PUBLISHED one may already sit on a paper, so editing creates a
    new version and marks the old row superseded. ``attempt_questions`` keeps
    pointing at the row it was served, so a finished exam never changes.

    Returns whichever variant the teacher should now be looking at.
    """
    if is_in_use(variant) or variant.validation_status.is_immutable:
        return _supersede(db, variant, changes, actor)

    for field_name, value in changes.items():
        setattr(variant, field_name, value)
    variant.content_hash = content_hash(variant.question_text, variant.options_json)

    if db.scalar(
        select(QuestionVariant.id).where(
            QuestionVariant.family_id == variant.family_id,
            QuestionVariant.content_hash == variant.content_hash,
            QuestionVariant.id != variant.id,
        )
    ):
        db.rollback()
        raise Conflict(
            "That edit would duplicate another variant in this question.",
            code="DUPLICATE_VARIANT",
        )

    db.commit()
    db.refresh(variant)
    return variant


def _supersede(
    db: Session, original: QuestionVariant, changes: dict[str, Any], actor: User
) -> QuestionVariant:
    """Create the next version of a variant and retire the current one."""
    question_text = changes.get("question_text", original.question_text)
    options = changes.get("options_json", original.options_json)
    digest = content_hash(question_text, options)

    if digest == original.content_hash:
        raise Conflict(
            "That edit does not change the question content.", code="NO_CHANGE"
        )
    if db.scalar(
        select(QuestionVariant.id).where(
            QuestionVariant.family_id == original.family_id,
            QuestionVariant.content_hash == digest,
        )
    ):
        raise Conflict(
            "That edit would duplicate another variant in this question.",
            code="DUPLICATE_VARIANT",
        )

    replacement = QuestionVariant(
        family_id=original.family_id,
        question_text=question_text,
        options_json=options,
        correct_option=changes.get("correct_option", original.correct_option),
        numeric_answer_json=changes.get("numeric_answer_json", original.numeric_answer_json),
        explanation=changes.get("explanation", original.explanation),
        generation_model=original.generation_model,
        generation_prompt_version=original.generation_prompt_version,
        generation_status=GenerationStatus.MANUAL,
        # A new version is a new question and needs approving on its own merits.
        validation_status=VariantStatus.VALIDATED,
        content_hash=digest,
        version=original.version + 1,
    )
    db.add(replacement)
    db.flush()

    original.superseded_by_id = replacement.id
    db.commit()
    db.refresh(replacement)

    logger.info(
        "Variant superseded",
        extra={
            "family_id": str(original.family_id),
            "original_variant_id": str(original.id),
            "replacement_variant_id": str(replacement.id),
            "version": replacement.version,
            "actor_id": str(actor.id),
        },
    )
    return replacement


def delete_variant(db: Session, variant: QuestionVariant) -> None:
    """Delete a variant that has never been served.

    A served variant is part of a student's record. Rejecting it removes it
    from future papers without destroying that record, so that is the route
    offered instead.
    """
    if is_in_use(variant):
        raise Conflict(
            "This variant has been used in a student's exam and cannot be deleted. "
            "Reject it instead to keep it out of future exams.",
            code="VARIANT_IN_USE",
        )
    if variant.validation_status is VariantStatus.PUBLISHED:
        raise Conflict(
            "A published variant cannot be deleted. Reject it instead.",
            code="VARIANT_PUBLISHED",
        )
    db.delete(variant)
    db.commit()


def approve_variant(db: Session, variant: QuestionVariant, actor: User) -> QuestionVariant:
    transition(variant, VariantStatus.APPROVED, actor=actor)
    db.commit()
    db.refresh(variant)
    logger.info(
        "Variant approved",
        extra={"variant_id": str(variant.id), "family_id": str(variant.family_id)},
    )
    return variant


def reject_variant(
    db: Session, variant: QuestionVariant, actor: User, reason: str | None = None
) -> QuestionVariant:
    # Rejecting a published variant is allowed and important: it stops the
    # variant appearing on new papers while leaving every historical paper
    # intact, because papers are frozen in attempt_questions.
    transition(variant, VariantStatus.REJECTED, actor=actor, reason=reason)
    db.commit()
    db.refresh(variant)
    return variant


def reinstate_variant(db: Session, variant: QuestionVariant, actor: User) -> QuestionVariant:
    """Send a rejected variant back for review."""
    transition(variant, VariantStatus.VALIDATED, actor=actor)
    db.commit()
    db.refresh(variant)
    return variant


def variants_for_family(
    db: Session, family_id: uuid.UUID, *, include_superseded: bool = True
) -> list[QuestionVariant]:
    statement = select(QuestionVariant).where(QuestionVariant.family_id == family_id)
    if not include_superseded:
        statement = statement.where(QuestionVariant.superseded_by_id.is_(None))
    return list(db.scalars(statement.order_by(QuestionVariant.created_at)))


__all__ = [
    "GenerationOutcome",
    "RejectedCandidate",
    "approve_variant",
    "create_manual_variant",
    "delete_variant",
    "edit_variant",
    "generate_variants",
    "reinstate_variant",
    "reject_variant",
    "variants_for_family",
]
