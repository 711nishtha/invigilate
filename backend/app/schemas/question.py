"""Question family and variant schemas.

The important split in this file is between teacher-facing and student-facing
shapes. ``VariantOut`` carries the answer key because a teacher reviewing a
question must see it. ``StudentQuestionOut`` has no field capable of holding
one, so a student response cannot leak the key even if a route handler passes
the wrong object — the serializer has nowhere to put it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field, model_validator

from app.db.models import GenerationStatus, QuestionType, VariantStatus
from app.schemas.common import APIModel, StrictModel

MAX_OPTIONS = 10


# --- Question families -----------------------------------------------------


class FamilyCreateRequest(StrictModel):
    original_question: str = Field(min_length=1, max_length=4000)
    question_type: QuestionType
    topic: str | None = Field(default=None, max_length=200)
    difficulty: str | None = Field(default=None, max_length=20)
    learning_objective: str | None = Field(default=None, max_length=2000)
    generation_config_json: dict[str, Any] = Field(default_factory=dict)
    position: int | None = Field(default=None, ge=0)


class FamilyUpdateRequest(StrictModel):
    original_question: str | None = Field(default=None, min_length=1, max_length=4000)
    question_type: QuestionType | None = None
    topic: str | None = Field(default=None, max_length=200)
    difficulty: str | None = Field(default=None, max_length=20)
    learning_objective: str | None = Field(default=None, max_length=2000)
    generation_config_json: dict[str, Any] | None = None
    position: int | None = Field(default=None, ge=0)

    def changes(self) -> dict[str, Any]:
        return self.model_dump(exclude_unset=True)


class FamilyReorderRequest(StrictModel):
    ordered_family_ids: list[uuid.UUID] = Field(min_length=1)


class VariantCountsOut(APIModel):
    """Per-family lifecycle tally, so the UI can show review progress."""

    generated: int = 0
    validated: int = 0
    approved: int = 0
    published: int = 0
    rejected: int = 0
    assignable: int = 0


class FamilyOut(APIModel):
    id: uuid.UUID
    quiz_id: uuid.UUID
    original_question: str
    question_type: QuestionType
    topic: str | None
    difficulty: str | None
    learning_objective: str | None
    generation_config_json: dict[str, Any]
    position: int
    created_at: datetime
    variant_counts: VariantCountsOut = Field(default_factory=VariantCountsOut)


# --- Variants: teacher-facing ---------------------------------------------


class VariantOut(APIModel):
    """Full variant, including the answer key.

    Teacher-only. Every route returning this must go through
    ``services.ownership``, which is what confines it to the quiz's owner.
    """

    id: uuid.UUID
    family_id: uuid.UUID
    question_text: str
    options_json: list[str] | None
    correct_option: int | None
    numeric_answer_json: dict[str, Any] | None
    explanation: str | None
    generation_model: str | None
    generation_prompt_version: str | None
    generation_status: GenerationStatus
    validation_status: VariantStatus
    validation_errors_json: list[dict[str, Any]] | None
    rejection_reason: str | None
    content_hash: str
    version: int
    superseded_by_id: uuid.UUID | None
    approved_at: datetime | None
    created_at: datetime
    is_assignable: bool = False
    in_use: bool = False


class VariantCreateRequest(StrictModel):
    question_text: str = Field(min_length=10, max_length=4000)
    options_json: list[str] | None = Field(default=None, max_length=MAX_OPTIONS)
    correct_option: int | None = Field(default=None, ge=0)
    numeric_answer_json: dict[str, Any] | None = None
    explanation: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def _check_shape(self) -> VariantCreateRequest:
        """A hand-written variant must still be answerable and unambiguous."""
        if self.options_json is not None:
            if len(self.options_json) < 2:
                raise ValueError("Provide at least two options")
            normalized = [option.strip().casefold() for option in self.options_json]
            if any(not option for option in normalized):
                raise ValueError("Options cannot be blank")
            if len(set(normalized)) != len(normalized):
                raise ValueError("Options must be distinct")
            if self.correct_option is None:
                raise ValueError("correct_option is required when options are given")
            if self.correct_option >= len(self.options_json):
                raise ValueError("correct_option is outside the options")
        elif self.numeric_answer_json is None:
            raise ValueError("Provide either options with a correct_option, or a numeric answer")
        return self


class VariantUpdateRequest(StrictModel):
    question_text: str | None = Field(default=None, min_length=10, max_length=4000)
    options_json: list[str] | None = Field(default=None, max_length=MAX_OPTIONS)
    correct_option: int | None = Field(default=None, ge=0)
    numeric_answer_json: dict[str, Any] | None = None
    explanation: str | None = Field(default=None, max_length=4000)

    def changes(self) -> dict[str, Any]:
        return self.model_dump(exclude_unset=True)


class VariantRejectRequest(StrictModel):
    reason: str | None = Field(default=None, max_length=1000)


class GenerateVariantsRequest(StrictModel):
    count: int | None = Field(default=None, ge=1, le=20)
    difficulty: str | None = Field(default=None, max_length=20)


class RejectedCandidateOut(APIModel):
    index: int
    reasons: list[dict[str, str]]


class GenerationResultOut(APIModel):
    """Outcome of a generation run, including partial failure."""

    family_id: str
    requested: int
    accepted: int
    rejected: list[RejectedCandidateOut]
    provider: str
    model: str | None
    prompt_version: str | None
    variants: list[VariantOut]
    error_code: str | None = None
    error_message: str | None = None


# --- Variants: student-facing ---------------------------------------------


class StudentQuestionOut(APIModel):
    """A question as a student receives it.

    Deliberately narrow. There is no field for ``correct_option``,
    ``numeric_answer_json``, ``explanation``, ``variant_id``,
    ``options_order_json``, ``content_hash``, ``validation_status`` or anything
    about generation. Phase 4's endpoints build this from the ORM row rather
    than serializing the row, so the answer key has no path to the wire.

    A regression test asserts this class has no answer-key field.
    """

    attempt_question_id: uuid.UUID
    position: int
    question_type: QuestionType
    question_text: str
    # Already permuted for this student; the permutation stays server-side.
    options: list[str] | None = None
    selected_answer: dict[str, Any] | None = None


# Field names that must never appear on a student-facing schema. Used by the
# non-disclosure regression test.
FORBIDDEN_STUDENT_FIELDS = frozenset(
    {
        "correct_option",
        "correct_answer",
        "numeric_answer_json",
        "numeric_answer",
        "explanation",
        "variant_id",
        "options_order_json",
        "content_hash",
        "validation_status",
        "validation_errors_json",
        "generation_config_json",
        "generation_prompt_version",
        "generation_model",
        "is_correct",
        "score",
        "rejection_reason",
    }
)
