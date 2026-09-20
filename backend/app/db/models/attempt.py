"""Attempts, the frozen paper, and answers.

An attempt is the authoritative record of one student's exam: when it started,
when it must end, exactly which variants they received in which order, and what
they answered.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, created_at_column, uuid_pk
from app.db.models.enums import AttemptStatus, IntegrityStatus
from app.db.types import BigInt, GUID, JSONColumn, pg_enum

if TYPE_CHECKING:
    from app.db.models.integrity import IntegrityEvent
    from app.db.models.question import QuestionVariant
    from app.db.models.quiz import Quiz
    from app.db.models.user import User


class Attempt(Base):
    __tablename__ = "attempts"

    id: Mapped[uuid.UUID] = uuid_pk()
    quiz_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("quizzes.id", ondelete="CASCADE"), nullable=False
    )
    student_user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    student_name: Mapped[str] = mapped_column(String(200), nullable=False)
    # Lowercased on write; the identity the one-attempt rule keys on.
    student_email: Mapped[str] = mapped_column(String(320), nullable=False)
    # Only the hash of the bearer token is stored.
    attempt_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # Server clock. Immutable after creation.
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    status: Mapped[AttemptStatus] = mapped_column(
        pg_enum(AttemptStatus, "attempt_status"), nullable=False, default=AttemptStatus.IN_PROGRESS
    )
    # Drives the seeded draw; stored so a paper can be explained after the fact.
    seed: Mapped[int] = mapped_column(BigInt, nullable=False)

    # Written once, at finalization, by the grading service.
    score: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)
    max_score: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)
    percentage: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)

    # Cumulative severity, not a count of events.
    integrity_violation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    integrity_status: Mapped[IntegrityStatus] = mapped_column(
        pg_enum(IntegrityStatus, "integrity_status"), nullable=False, default=IntegrityStatus.CLEAN
    )
    # Suppresses duplicate result emails on a repeated submit.
    results_emailed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = created_at_column()

    quiz: Mapped["Quiz"] = relationship(back_populates="attempts")
    student_user: Mapped["User | None"] = relationship()
    questions: Mapped[list["AttemptQuestion"]] = relationship(
        back_populates="attempt",
        cascade="all, delete-orphan",
        order_by="AttemptQuestion.position",
    )
    integrity_events: Mapped[list["IntegrityEvent"]] = relationship(
        back_populates="attempt",
        cascade="all, delete-orphan",
        order_by="IntegrityEvent.occurred_at",
    )

    __table_args__ = (
        # One attempt per student per quiz, enforced by the database so that two
        # simultaneous starts cannot both succeed.
        UniqueConstraint("quiz_id", "student_email", name="uq_attempts_quiz_id_student_email"),
        Index("ix_attempts_quiz_id_status", "quiz_id", "status"),
        Index("ix_attempts_attempt_token_hash", "attempt_token_hash"),
    )

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Attempt {self.id} {self.status.value}>"


class AttemptQuestion(Base):
    """One question as this student received it. Written once, never regenerated."""

    __tablename__ = "attempt_questions"

    id: Mapped[uuid.UUID] = uuid_pk()
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("attempts.id", ondelete="CASCADE"), nullable=False
    )
    variant_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("question_variants.id", ondelete="RESTRICT"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    # Permutation: displayed index -> canonical index in variant.options_json.
    # Kept server-side so a student cannot infer the key by replaying requests.
    options_order_json: Mapped[list[int] | None] = mapped_column(JSONColumn, nullable=True)

    attempt: Mapped["Attempt"] = relationship(back_populates="questions")
    variant: Mapped["QuestionVariant"] = relationship(back_populates="attempt_questions")
    answer: Mapped["Answer | None"] = relationship(
        back_populates="attempt_question", cascade="all, delete-orphan", uselist=False
    )

    __table_args__ = (
        UniqueConstraint("attempt_id", "position", name="uq_attempt_questions_attempt_id_position"),
        UniqueConstraint(
            "attempt_id", "variant_id", name="uq_attempt_questions_attempt_id_variant_id"
        ),
    )


class Answer(Base):
    __tablename__ = "answers"

    id: Mapped[uuid.UUID] = uuid_pk()
    # Unique: an answer is upserted, never duplicated.
    attempt_question_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("attempt_questions.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    # {"displayed_index": int} | {"value": float} | {"text": str}
    selected_answer: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False)
    # NULL until finalization. Correctness is not in the database during the
    # exam, so it cannot leak during the exam.
    is_correct: Mapped[bool | None] = mapped_column(nullable=True)
    answered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    attempt_question: Mapped["AttemptQuestion"] = relationship(back_populates="answer")
