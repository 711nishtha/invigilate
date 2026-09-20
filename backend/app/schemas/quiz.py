"""Quiz request and response schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field, field_validator

from app.db.models import QuizStatus, SecurityLevel
from app.schemas.common import APIModel, StrictModel

MIN_DURATION_SECONDS = 60
MAX_DURATION_SECONDS = 28_800


class QuizCreateRequest(StrictModel):
    title: str = Field(min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=5000)
    duration_seconds: int = Field(ge=MIN_DURATION_SECONDS, le=MAX_DURATION_SECONDS)
    question_count: int = Field(ge=1, le=200)
    security_level: SecurityLevel = SecurityLevel.SECURE
    max_integrity_violations: int = Field(default=2, ge=0, le=100)
    randomization_enabled: bool = True
    allow_backtracking: bool = True
    show_score_after_submission: bool = False
    coverage_config_json: dict[str, Any] = Field(default_factory=dict)

    @field_validator("coverage_config_json")
    @classmethod
    def _validate_coverage(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Only the coverage keys the publish rules understand.

        Silently storing an unrecognised key would let a teacher believe a
        requirement is enforced when nothing reads it.
        """
        allowed = {"required_topics", "required_difficulties", "required_objectives"}
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"Unsupported coverage keys: {', '.join(sorted(unknown))}")
        for key, entries in value.items():
            if not isinstance(entries, list) or not all(isinstance(e, str) for e in entries):
                raise ValueError(f"'{key}' must be a list of strings")
        return value


class QuizUpdateRequest(StrictModel):
    """Every field optional; only what is sent is changed."""

    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=5000)
    duration_seconds: int | None = Field(
        default=None, ge=MIN_DURATION_SECONDS, le=MAX_DURATION_SECONDS
    )
    question_count: int | None = Field(default=None, ge=1, le=200)
    security_level: SecurityLevel | None = None
    max_integrity_violations: int | None = Field(default=None, ge=0, le=100)
    randomization_enabled: bool | None = None
    allow_backtracking: bool | None = None
    show_score_after_submission: bool | None = None
    coverage_config_json: dict[str, Any] | None = None

    _validate_coverage = field_validator("coverage_config_json")(
        QuizCreateRequest._validate_coverage.__func__  # type: ignore[attr-defined]
    )

    def changes(self) -> dict[str, Any]:
        """Only the fields the caller actually sent."""
        return self.model_dump(exclude_unset=True, exclude_none=True)


class BlockerOut(APIModel):
    code: str
    message: str
    family_id: str | None = None
    variant_id: str | None = None


class PublishReadinessOut(APIModel):
    """Why a quiz can or cannot be published. Teacher-facing."""

    publishable: bool
    blockers: list[BlockerOut]
    family_count: int
    ready_family_count: int
    assignable_variant_count: int
    question_count: int
    distinct_paper_estimate: int


class QuizOut(APIModel):
    id: uuid.UUID
    title: str
    description: str | None
    duration_seconds: int
    question_count: int
    security_level: SecurityLevel
    max_integrity_violations: int
    randomization_enabled: bool
    allow_backtracking: bool
    show_score_after_submission: bool
    coverage_config_json: dict[str, Any]
    status: QuizStatus
    public_code: str | None
    created_at: datetime
    published_at: datetime | None


class QuizSummaryOut(QuizOut):
    """List-row shape: the quiz plus the counters the list screen shows."""

    family_count: int = 0
    attempt_count: int = 0
    assignable_variant_count: int = 0


class QuizDetailOut(QuizOut):
    readiness: PublishReadinessOut
    stats: dict[str, int]
