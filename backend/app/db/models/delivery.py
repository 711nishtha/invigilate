"""Result delivery records.

A separate table rather than columns on ``quizzes``, for one reason: a result
email is an operational side effect with its own lifecycle, retries and failure
history, while a quiz is academic record. Overloading the quiz row would mean a
failed send mutated exam data, and would lose the history of which attempts a
given email covered.

The invariant this table exists to protect: **a delivery failure never alters a
result.** Rows here are written after the results are already durable, and
nothing in this table is read when grading or finalizing an attempt.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, created_at_column, uuid_pk
from app.db.types import GUID, JSONColumn, pg_enum

if TYPE_CHECKING:
    from app.db.models.quiz import Quiz
    from app.db.models.user import User


class DeliveryStatus(str, enum.Enum):
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"


class DeliveryKind(str, enum.Enum):
    RESULTS_EMAIL = "RESULTS_EMAIL"


class ResultDelivery(Base):
    """One attempt to deliver a quiz's results to its owner."""

    __tablename__ = "result_deliveries"

    id: Mapped[uuid.UUID] = uuid_pk()
    quiz_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("quizzes.id", ondelete="CASCADE"), nullable=False
    )
    requested_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[DeliveryKind] = mapped_column(
        pg_enum(DeliveryKind, "delivery_kind"),
        nullable=False,
        default=DeliveryKind.RESULTS_EMAIL,
        server_default=DeliveryKind.RESULTS_EMAIL.value,
    )
    status: Mapped[DeliveryStatus] = mapped_column(
        pg_enum(DeliveryStatus, "delivery_status"),
        nullable=False,
        default=DeliveryStatus.PENDING,
        server_default=DeliveryStatus.PENDING.value,
    )
    # Recorded so "who received this?" stays answerable months later, even if
    # the teacher has since changed their address.
    recipient: Mapped[str] = mapped_column(String(320), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # How many attempts the report covered, so a teacher can tell a stale
    # email from a current one.
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # Error class and a truncated detail. Never an API key, never the body.
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts_made: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # Where the workbook was stored, when storage is configured. Null means the
    # export was streamed to the teacher and not retained.
    export_object_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict, server_default=text("'{}'")
    )

    created_at: Mapped[datetime] = created_at_column()
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    quiz: Mapped["Quiz"] = relationship()
    requested_by: Mapped["User | None"] = relationship()

    __table_args__ = (Index("ix_result_deliveries_quiz_id_created_at", "quiz_id", "created_at"),)

    @property
    def is_terminal(self) -> bool:
        return self.status in (DeliveryStatus.SENT, DeliveryStatus.FAILED)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ResultDelivery {self.id} {self.status.value}>"
