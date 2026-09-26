"""Proctoring request and response schemas.

The most important thing about the request models here is what they do **not**
have. There is no ``severity``, no ``violation_count``, no ``suspicion_score``,
no ``verdict``, no ``ready`` and no ``is_cheating``. Those are server-side
conclusions, and a client that tries to send one gets a 422 rather than being
quietly ignored, because ``StrictModel`` forbids extra fields.

What a client may send is *observations*: "the camera track is live", "the face
detector found one face". The server decides what those are worth.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field, field_validator

from app.db.models import (
    EvidenceKind,
    IntegrityStatus,
    PrecheckStatus,
    ProctoringMode,
    ProctoringReview,
    ProctoringStatus,
)
from app.schemas.common import APIModel, StrictModel

# --- Pre-exam check ----------------------------------------------------------


class PrecheckStartRequest(StrictModel):
    student_name: str = Field(min_length=1, max_length=200)
    student_email: str = Field(min_length=3, max_length=320)


class PrecheckStartResponse(APIModel):
    precheck_id: uuid.UUID
    # Shown once; only its hash is stored, as with an attempt token.
    precheck_token: str
    status: PrecheckStatus
    mode: ProctoringMode
    server_time: datetime
    expires_at: datetime
    # What the browser must arrange. Derived from the server-side policy; the
    # browser is told what to prepare, never asked what the policy is.
    requirements: dict[str, Any]
    # The honest capability inventory for this mode, straight from the lockdown
    # provider. Rendered to the student verbatim.
    monitoring_notice: list[str]


class CapabilityReport(StrictModel):
    """What the browser detected that it can do.

    Every field is a capability question ("is getUserMedia available?"), never
    a state question ("is the camera on?"). State belongs in readiness.
    """

    camera: bool = False
    microphone: bool = False
    screen_share: bool = False
    fullscreen: bool = False
    # The Shape Detection API. Available in some Chromium builds and nowhere
    # else, which is exactly why it is detected rather than assumed.
    face_detection: bool = False
    object_detection: bool = False
    media_recorder: bool = False
    secure_context: bool = True
    is_mobile: bool = False
    browser_name: str | None = Field(default=None, max_length=100)
    browser_version: str | None = Field(default=None, max_length=50)
    platform: str | None = Field(default=None, max_length=100)


class CapabilityResponse(APIModel):
    precheck_id: uuid.UUID
    status: PrecheckStatus
    satisfiable: bool
    missing: list[str]
    notes: list[str]


class ReadinessReport(StrictModel):
    """What the browser currently observes.

    Note the absence of a ``ready`` field. There is no way to express a verdict
    in this model, so the only thing that can produce one is
    ``proctoring_policy.evaluate_readiness`` on the server.
    """

    camera_active: bool = False
    microphone_active: bool = False
    screen_share_active: bool = False
    fullscreen_active: bool = False
    face_detected: bool = False
    multiple_faces: bool = False
    consent_accepted: bool = False
    # A client saying "my monitoring is broken" blocks the start. Honesty is
    # never punished here, but it is also never waved through.
    monitoring_impaired: bool = False
    impaired_reason: str | None = Field(default=None, max_length=200)


class ReadinessResponse(APIModel):
    precheck_id: uuid.UUID
    status: PrecheckStatus
    ready: bool
    checks: dict[str, bool]
    failures: list[str]
    server_time: datetime
    expires_at: datetime


class PrecheckStartExamRequest(StrictModel):
    """Deliberately empty.

    The start request carries no arguments at all, because there is nothing a
    client could usefully say at this point that the server would believe. It
    exists as a model so the endpoint still rejects a body with content.
    """


# --- During the exam ---------------------------------------------------------


class ProctoringHeartbeatRequest(StrictModel):
    """One periodic report of observed monitoring state."""

    camera_active: bool = False
    microphone_active: bool = False
    screen_share_active: bool = False
    fullscreen_active: bool = False
    monitoring_impaired: bool = False
    impaired_reason: str | None = Field(default=None, max_length=200)
    client_reported_at: datetime | None = None


class ProctoringHeartbeatResponse(APIModel):
    acknowledged: bool
    server_time: datetime
    proctoring_status: ProctoringStatus
    integrity_status: IntegrityStatus
    violation_total: int
    max_integrity_violations: int
    # Next heartbeat due in this many seconds, from the server's policy. The
    # client does not choose its own cadence.
    next_heartbeat_seconds: int
    signals_recorded: list[str]
    warnings: list[str]
    auto_submitted: bool
    attempt_status: str


class EvidenceUploadResponse(APIModel):
    stored: bool
    evidence_id: uuid.UUID | None = None
    kind: EvidenceKind | None = None
    reason: str | None = None
    # Never a URL. A student uploads evidence; they do not get to read it back.
    retention_expires_at: datetime | None = None


# --- Teacher review ----------------------------------------------------------


class EvidenceOut(APIModel):
    id: uuid.UUID
    kind: EvidenceKind
    captured_at: datetime
    content_type: str
    size_bytes: int
    retention_expires_at: datetime
    # Short-lived, minted per request after an ownership check. Absent when the
    # object has been purged or storage is unreachable.
    download_url: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProctoringEventOut(APIModel):
    id: uuid.UUID
    event_type: str
    severity: int
    occurred_at: datetime
    action_taken: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProctoringSummaryOut(APIModel):
    mode: ProctoringMode
    proctoring_status: ProctoringStatus
    review_state: ProctoringReview
    outcome: str
    headline: str
    # Shown next to the numbers, not buried in a help page.
    caveats: list[str]
    signal_counts: dict[str, int]
    violation_total: int
    max_violations: int
    evidence_count: int
    auto_submitted: bool


class ProctoringDetailOut(APIModel):
    attempt_id: uuid.UUID
    student_name: str
    student_email: str
    summary: ProctoringSummaryOut
    # The provider's own statement of what it can and cannot enforce.
    monitoring_capabilities: dict[str, Any]
    precheck: dict[str, Any] | None
    events: list[ProctoringEventOut]
    evidence: list[EvidenceOut]
    last_heartbeat_at: datetime | None
    server_time: datetime


class ProctoringReviewRequest(StrictModel):
    """A teacher's conclusion.

    ``ProctoringReview`` has no "cheating" member, so this endpoint cannot be
    used to record that finding. Academic misconduct is a process that happens
    outside a quiz platform, and a dropdown here would misrepresent it.
    """

    decision: ProctoringReview
    note: str | None = Field(default=None, max_length=2000)

    @field_validator("decision")
    @classmethod
    def _must_be_a_decision(cls, value: ProctoringReview) -> ProctoringReview:
        if value is ProctoringReview.PENDING:
            raise ValueError("PENDING is the initial state, not a review decision.")
        return value


__all__ = [
    "CapabilityReport",
    "CapabilityResponse",
    "EvidenceOut",
    "EvidenceUploadResponse",
    "PrecheckStartExamRequest",
    "PrecheckStartRequest",
    "PrecheckStartResponse",
    "ProctoringDetailOut",
    "ProctoringEventOut",
    "ProctoringHeartbeatRequest",
    "ProctoringHeartbeatResponse",
    "ProctoringReviewRequest",
    "ProctoringSummaryOut",
    "ReadinessReport",
    "ReadinessResponse",
]
