"""Quiz model — the teacher-configured exam definition."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, created_at_column, uuid_pk
from app.db.models.enums import QuizStatus, SecurityLevel
from app.db.types import GUID, pg_enum

if TYPE_CHECKING:
    from app.db.models.attempt import Attempt
    from app.db.models.question import QuestionFamily
    from app.db.models.user import User


class Quiz(Base):
    __tablename__ = "quizzes"

    id: Mapped[uuid.UUID] = uuid_pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # Assigned at publish, not at creation: a draft has no public surface.
    public_code: Mapped[str | None] = mapped_column(String(16), unique=True, nullable=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    # How many questions each student receives, which may be fewer than the
    # number of question families the quiz holds.
    question_count: Mapped[int] = mapped_column(Integer, nullable=False)
    security_level: Mapped[SecurityLevel] = mapped_column(
        pg_enum(SecurityLevel, "security_level"), nullable=False, default=SecurityLevel.SECURE
    )
    # Cumulative severity budget, not a count of events.
    max_integrity_violations: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    randomization_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    allow_backtracking: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    show_score_after_submission: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    status: Mapped[QuizStatus] = mapped_column(
        pg_enum(QuizStatus, "quiz_status"), nullable=False, default=QuizStatus.DRAFT
    )
    created_at: Mapped[datetime] = created_at_column()
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    owner: Mapped["User"] = relationship(back_populates="quizzes")
    question_families: Mapped[list["QuestionFamily"]] = relationship(
        back_populates="quiz", cascade="all, delete-orphan", order_by="QuestionFamily.position"
    )
    attempts: Mapped[list["Attempt"]] = relationship(
        back_populates="quiz", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "duration_seconds >= 60 AND duration_seconds <= 28800", name="duration_range"
        ),
        CheckConstraint("question_count >= 1 AND question_count <= 200", name="question_count_range"),
        CheckConstraint("max_integrity_violations >= 0", name="max_violations_non_negative"),
        Index("ix_quizzes_owner_id_created_at", "owner_id", "created_at"),
    )

    @property
    def is_open_for_attempts(self) -> bool:
        return self.status == QuizStatus.PUBLISHED

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Quiz {self.title!r} status={self.status.value}>"
