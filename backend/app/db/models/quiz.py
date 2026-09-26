"""Quiz model — the teacher-configured exam definition."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    false,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, created_at_column, uuid_pk
from app.db.models.enums import ProctoringMode, QuizStatus, SecurityLevel
from app.db.types import GUID, JSONColumn, pg_enum

if TYPE_CHECKING:
    from app.db.models.attempt import Attempt
    from app.db.models.proctoring import PrecheckSession
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
        pg_enum(SecurityLevel, "security_level"),
        nullable=False,
        default=SecurityLevel.SECURE,
        server_default=SecurityLevel.SECURE.value,
    )
    # Cumulative severity budget, not a count of events.
    max_integrity_violations: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2, server_default="2"
    )
    randomization_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    allow_backtracking: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    show_score_after_submission: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    # --- Phase 5: proctoring -------------------------------------------------
    # Orthogonal to security_level, which governs in-page browser behaviour.
    # This governs whether a *pre-exam security check* is mandatory and whether
    # the server will refuse to start an attempt until it has passed.
    proctoring_mode: Mapped[ProctoringMode] = mapped_column(
        pg_enum(ProctoringMode, "proctoring_mode"),
        nullable=False,
        default=ProctoringMode.STANDARD,
        server_default=ProctoringMode.STANDARD.value,
    )
    # Teacher overrides on top of the mode's defaults. Validated against
    # services.proctoring_policy.CONFIGURABLE_KEYS on write, so an unrecognised
    # key is refused rather than stored and never read.
    proctoring_config_json: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict, server_default=text("'{}'")
    )

    status: Mapped[QuizStatus] = mapped_column(
        pg_enum(QuizStatus, "quiz_status"),
        nullable=False,
        default=QuizStatus.DRAFT,
        server_default=QuizStatus.DRAFT.value,
    )
    # Optional publish requirements, e.g.
    # {"required_topics": ["Trees"], "required_difficulties": ["hard"]}.
    # Empty by default so a quiz is not boxed in by assumptions made here; the
    # assignment engine reads the same keys when selecting variants.
    coverage_config_json: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict, server_default=text("'{}'")
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
    precheck_sessions: Mapped[list["PrecheckSession"]] = relationship(
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
