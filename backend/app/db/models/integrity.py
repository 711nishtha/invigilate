"""Integrity event log — the auditable timeline of an attempt."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, uuid_pk
from app.db.models.enums import IntegrityAction, IntegrityEventType
from app.db.types import GUID, JSONColumn, pg_enum

if TYPE_CHECKING:
    from app.db.models.attempt import Attempt


class IntegrityEvent(Base):
    __tablename__ = "integrity_events"

    id: Mapped[uuid.UUID] = uuid_pk()
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("attempts.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[IntegrityEventType] = mapped_column(
        pg_enum(IntegrityEventType, "integrity_event_type"), nullable=False
    )
    # Assigned server-side from the policy table. A client-supplied severity is
    # discarded.
    severity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Server receipt time — the authoritative ordering of the timeline.
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Kept for forensics only; never used for a decision.
    client_reported_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False, default=dict)
    action_taken: Mapped[IntegrityAction] = mapped_column(
        pg_enum(IntegrityAction, "integrity_action"), nullable=False, default=IntegrityAction.LOGGED
    )

    attempt: Mapped["Attempt"] = relationship(back_populates="integrity_events")

    __table_args__ = (
        Index("ix_integrity_events_attempt_id_occurred_at", "attempt_id", "occurred_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<IntegrityEvent {self.event_type.value} sev={self.severity}>"
