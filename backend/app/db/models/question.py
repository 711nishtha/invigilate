"""Question families and their variants.

A family is one original question. A variant is one concrete rendering of it.
A student's paper draws at most one variant per family, which is what makes the
papers differ while the assessment stays equivalent.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, created_at_column, uuid_pk
from app.db.models.enums import GenerationStatus, QuestionType, ValidationStatus
from app.db.types import GUID, JSONColumn, pg_enum

if TYPE_CHECKING:
    from app.db.models.attempt import AttemptQuestion
    from app.db.models.quiz import Quiz


class QuestionFamily(Base):
    __tablename__ = "question_families"

    id: Mapped[uuid.UUID] = uuid_pk()
    quiz_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("quizzes.id", ondelete="CASCADE"), nullable=False
    )
    original_question: Mapped[str] = mapped_column(Text, nullable=False)
    question_type: Mapped[QuestionType] = mapped_column(
        pg_enum(QuestionType, "question_type"), nullable=False
    )
    topic: Mapped[str | None] = mapped_column(String(200), nullable=True)
    difficulty: Mapped[str | None] = mapped_column(String(20), nullable=True)
    learning_objective: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Variant count, numeric template, parameter ranges, dedup threshold.
    generation_config_json: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = created_at_column()

    quiz: Mapped["Quiz"] = relationship(back_populates="question_families")
    variants: Mapped[list["QuestionVariant"]] = relationship(
        back_populates="family", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_question_families_quiz_id_position", "quiz_id", "position"),)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<QuestionFamily {self.question_type.value} pos={self.position}>"


class QuestionVariant(Base):
    __tablename__ = "question_variants"

    id: Mapped[uuid.UUID] = uuid_pk()
    family_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("question_families.id", ondelete="CASCADE"), nullable=False
    )
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    # Canonical option order. What a student sees is a permutation of this,
    # recorded per attempt in attempt_questions.options_order_json.
    options_json: Mapped[list[str] | None] = mapped_column(JSONColumn, nullable=True)
    # The answer key. Never present in any student-facing schema.
    correct_option: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # {"value": float, "tolerance": float, "unit": str} for NUMERICAL.
    numeric_answer_json: Mapped[dict[str, Any] | None] = mapped_column(JSONColumn, nullable=True)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    generation_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    generation_prompt_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    generation_status: Mapped[GenerationStatus] = mapped_column(
        pg_enum(GenerationStatus, "generation_status"),
        nullable=False,
        default=GenerationStatus.AI_GENERATED,
    )
    validation_status: Mapped[ValidationStatus] = mapped_column(
        pg_enum(ValidationStatus, "validation_status"),
        nullable=False,
        default=ValidationStatus.PENDING,
    )
    # SHA-256 of normalized text + sorted options. Backs exact-duplicate
    # rejection at the database level.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = created_at_column()

    family: Mapped["QuestionFamily"] = relationship(back_populates="variants")
    attempt_questions: Mapped[list["AttemptQuestion"]] = relationship(back_populates="variant")

    __table_args__ = (
        UniqueConstraint("family_id", "content_hash", name="uq_question_variants_family_id_content_hash"),
        CheckConstraint(
            "correct_option IS NULL OR correct_option >= 0", name="correct_option_non_negative"
        ),
        # The hot path when assembling a paper.
        Index("ix_question_variants_family_id_validation_status", "family_id", "validation_status"),
    )

    @property
    def is_approved(self) -> bool:
        return self.validation_status == ValidationStatus.APPROVED

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<QuestionVariant {self.id} {self.validation_status.value}>"
