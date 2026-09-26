"""Pre-exam security checks and proctoring evidence.

Two tables, and the important one is the first.

``precheck_sessions`` exists so that a proctored exam has somewhere to live
*before* the attempt does. A student granting camera permission, fixing their
lighting or finding a quiet room is not sitting an exam, and no clock should be
running while they do it. The attempt — and therefore ``started_at``,
``expires_at`` and the frozen paper — is created only when the server has
decided the checks passed.

That ordering is also the authorization boundary. The browser cannot create an
attempt for a proctored quiz at all; it can only create a precheck session,
report observations into it, and ask the server to start. The decision is made
on the server from a policy the browser never sees in full.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, created_at_column, uuid_pk
from app.db.models.enums import EvidenceKind, EvidenceSource, PrecheckStatus, ProctoringMode
from app.db.types import GUID, JSONColumn, pg_enum

if TYPE_CHECKING:
    from app.db.models.attempt import Attempt
    from app.db.models.integrity import IntegrityEvent
    from app.db.models.quiz import Quiz


class PrecheckSession(Base):
    """One student's pre-exam security check for one quiz.

    Short-lived and single-use: it reaches READY only by a server decision, and
    CONSUMED exactly once, when it produces an attempt.
    """

    __tablename__ = "precheck_sessions"

    id: Mapped[uuid.UUID] = uuid_pk()
    quiz_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("quizzes.id", ondelete="CASCADE"), nullable=False
    )
    # Identity is captured here rather than at start, so the one-attempt rule
    # can be pre-checked before a student spends two minutes on permissions.
    student_name: Mapped[str] = mapped_column(String(200), nullable=False)
    student_email: Mapped[str] = mapped_column(String(320), nullable=False)
    # Only the hash of the bearer token is stored, as with attempt tokens.
    session_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    status: Mapped[PrecheckStatus] = mapped_column(
        pg_enum(PrecheckStatus, "precheck_status"),
        nullable=False,
        default=PrecheckStatus.PENDING,
        server_default=PrecheckStatus.PENDING.value,
    )
    # Copied from the quiz at creation: the requirements a student was shown
    # must not change underneath them mid-check.
    proctoring_mode: Mapped[ProctoringMode] = mapped_column(
        pg_enum(ProctoringMode, "proctoring_mode"),
        nullable=False,
        default=ProctoringMode.STANDARD,
        server_default=ProctoringMode.STANDARD.value,
    )

    # Raw, untrusted, kept verbatim for forensics. Decisions are made from the
    # verdicts below, which the server computes.
    capabilities_json: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict, server_default=text("'{}'")
    )
    observations_json: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict, server_default=text("'{}'")
    )
    # The server's own record of what it decided and why. This is what a
    # teacher reads; it is never written from a request body.
    verdict_json: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict, server_default=text("'{}'")
    )

    consent_accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Set only by the server, only in the READY transition.
    authorized_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # A READY session is not a permanent licence to start; it goes stale.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("attempts.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = created_at_column()

    quiz: Mapped["Quiz"] = relationship(back_populates="precheck_sessions")

    __table_args__ = (
        Index("ix_precheck_sessions_token_hash", "session_token_hash"),
        Index("ix_precheck_sessions_quiz_id_email", "quiz_id", "student_email"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<PrecheckSession {self.id} {self.status.value}>"


class ProctoringEvidence(Base):
    """A pointer to one stored artefact, plus what the server knows about it.

    The bytes live in a **private** bucket; this row holds the path, never a
    public URL. Teachers read it through a short-lived signed URL minted after
    an ownership check, so an evidence path leaking is not the same as the
    evidence leaking.

    ``retention_expires_at`` is written at capture, not bolted on later, so
    every object has a deletion date from the moment it exists.
    """

    __tablename__ = "proctoring_evidence"

    id: Mapped[uuid.UUID] = uuid_pk()
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("attempts.id", ondelete="CASCADE"), nullable=False
    )
    # The signal this artefact backs up, when there is one.
    integrity_event_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("integrity_events.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[EvidenceKind] = mapped_column(
        pg_enum(EvidenceKind, "evidence_kind"), nullable=False
    )
    source: Mapped[EvidenceSource] = mapped_column(
        pg_enum(EvidenceSource, "evidence_source"),
        nullable=False,
        default=EvidenceSource.CLIENT_CLAIM,
        server_default=EvidenceSource.CLIENT_CLAIM.value,
    )
    bucket: Mapped[str] = mapped_column(String(100), nullable=False)
    object_path: Mapped[str] = mapped_column(String(500), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    # Server receipt time. The client's own timestamp, if any, goes in metadata.
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retention_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict, server_default=text("'{}'")
    )

    attempt: Mapped["Attempt"] = relationship(back_populates="evidence")
    integrity_event: Mapped["IntegrityEvent | None"] = relationship()

    __table_args__ = (
        Index("ix_proctoring_evidence_attempt_id_captured_at", "attempt_id", "captured_at"),
        Index("ix_proctoring_evidence_retention_expires_at", "retention_expires_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ProctoringEvidence {self.kind.value} {self.object_path}>"
