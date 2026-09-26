"""Proctoring evidence: capture, storage, access and retention.

Evidence is the only thing that makes a proctoring signal reviewable. A log line
saying "no face detected at 14:32" is an assertion by an untrusted browser; a
still image at 14:32 is something a human can look at and disagree with. That is
the whole reason this module exists, and it is also why the defaults here are
conservative.

Design rules, in the order they matter:

1. **The bucket is private.** Nothing here ever produces a public URL. Reads go
   through a short-lived signed URL minted only after an ownership check, so a
   leaked object path is not a leaked recording.

2. **Stills by default, video only on purpose.** ``store_evidence`` captures
   snapshots tied to specific signals. Continuous recording is off unless a
   teacher turns it on, because a thirty-minute video of a student's bedroom is
   a liability that a four-frame sequence around an event is not.

3. **Retention is set at capture.** ``retention_expires_at`` is written on the
   row when the object is created, from the quiz's policy. Nothing is stored
   without a deletion date, and purging is a query rather than an archaeology
   project.

4. **A storage failure never costs a result.** Capture is best-effort: if the
   upload fails, the signal is still recorded, the exam continues, and the
   evidence row is simply absent. An exam is not rolled back because a bucket
   was unreachable.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.config import settings
from app.core.logging import Event, log_event
from app.db.models import (
    Attempt,
    EvidenceKind,
    EvidenceSource,
    IntegrityEvent,
    ProctoringEvidence,
)
from app.services.proctoring_policy import ProctoringPolicy
from app.storage.base import StorageError, StorageProvider
from app.storage.factory import get_storage_provider

# A still frame, not a film. The endpoint is authenticated by an attempt token
# alone, so it must not double as free object storage.
#
# These are ceilings, not the effective limit. ``BodySizeLimitMiddleware``
# rejects any request over ``MAX_REQUEST_BYTES`` (256 KB by default) before a
# route ever runs, so ``max_bytes_for`` takes the smaller of the two. Declaring
# a 512 KB limit here and letting the middleware return 413 at 256 KB would be
# a limit that documents itself incorrectly.
#
# A consequence worth stating: clip upload is unreachable at the default
# MAX_REQUEST_BYTES. Active clip recording is not shipped (see
# docs/proctoring.md), and enabling it would require raising that setting
# deliberately rather than discovering the ceiling in production.
MAX_SNAPSHOT_BYTES = 512 * 1024
MAX_CLIP_BYTES = 8 * 1024 * 1024

ALLOWED_CONTENT_TYPES = {
    EvidenceKind.PRECHECK_SNAPSHOT: {"image/jpeg", "image/png", "image/webp"},
    EvidenceKind.EVENT_SNAPSHOT: {"image/jpeg", "image/png", "image/webp"},
    EvidenceKind.CAMERA_CLIP: {"video/webm", "video/mp4"},
    EvidenceKind.SCREEN_CLIP: {"video/webm", "video/mp4"},
}

# How long a teacher's download link lives. Long enough to click, short enough
# that a copied URL in a chat log stops working.
SIGNED_URL_TTL_SECONDS = 300


class EvidenceRejected(Exception):
    """The artefact is not something this endpoint accepts."""


def max_bytes_for(kind: EvidenceKind) -> int:
    """The limit that actually applies, not the one this module would prefer.

    Capped by the global request-body limit so the two can never disagree.
    """
    declared = (
        MAX_CLIP_BYTES
        if kind in (EvidenceKind.CAMERA_CLIP, EvidenceKind.SCREEN_CLIP)
        else MAX_SNAPSHOT_BYTES
    )
    return min(declared, settings.max_request_bytes)


def object_path(attempt_id: uuid.UUID, kind: EvidenceKind, extension: str) -> str:
    """Namespaced by attempt, unguessable within it.

    The random component matters because the path is the only thing between an
    object and a request for it, and attempt ids appear in logs.
    """
    return f"attempts/{attempt_id}/{kind.value.lower()}/{uuid.uuid4().hex}.{extension}"


def _extension_for(content_type: str) -> str:
    return {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "video/webm": "webm",
        "video/mp4": "mp4",
    }.get(content_type, "bin")


@dataclass(frozen=True)
class CapturedEvidence:
    evidence: ProctoringEvidence | None
    stored: bool
    reason: str | None = None


def validate_artefact(kind: EvidenceKind, content_type: str, size_bytes: int) -> None:
    """Refuse anything that is not the artefact this kind is meant to be."""
    allowed = ALLOWED_CONTENT_TYPES.get(kind, set())
    if content_type not in allowed:
        raise EvidenceRejected(
            f"{kind.value} must be one of: {', '.join(sorted(allowed))}."
        )
    limit = max_bytes_for(kind)
    if size_bytes <= 0:
        raise EvidenceRejected("The uploaded file is empty.")
    if size_bytes > limit:
        raise EvidenceRejected(f"That file is larger than the {limit // 1024} KB limit.")


async def capture(
    db: Session,
    attempt: Attempt,
    policy: ProctoringPolicy,
    *,
    kind: EvidenceKind,
    content: bytes,
    content_type: str,
    integrity_event: IntegrityEvent | None = None,
    metadata: dict | None = None,
    provider: StorageProvider | None = None,
) -> CapturedEvidence:
    """Store one artefact and record where it went.

    Returns rather than raises on a storage failure. The caller has usually
    already recorded the signal this backs up, and losing the picture must not
    lose the timeline entry, let alone the exam.
    """
    if not policy.store_evidence:
        return CapturedEvidence(evidence=None, stored=False, reason="EVIDENCE_DISABLED")

    validate_artefact(kind, content_type, len(content))

    bucket = settings.supabase_storage_bucket_evidence
    path = object_path(attempt.id, kind, _extension_for(content_type))
    storage = provider or get_storage_provider()

    try:
        stored = await storage.upload(
            bucket=bucket, path=path, content=content, content_type=content_type
        )
    except StorageError as exc:
        # Best effort by design. The signal survives; the picture does not.
        log_event(
            Event.EVIDENCE_STORE_FAILED,
            attempt_id=str(attempt.id),
            kind=kind.value,
            retryable=exc.retryable,
            reason=exc.message,
        )
        return CapturedEvidence(evidence=None, stored=False, reason="STORAGE_UNAVAILABLE")

    now = utcnow()
    evidence = ProctoringEvidence(
        attempt_id=attempt.id,
        integrity_event_id=integrity_event.id if integrity_event is not None else None,
        kind=kind,
        # An artefact produced by the student's own browser is a claim about
        # the room, not an observation by the server. Labelling it accurately
        # is what keeps a review honest.
        source=EvidenceSource.CLIENT_CLAIM,
        bucket=stored.bucket,
        object_path=stored.path,
        content_type=content_type,
        size_bytes=stored.size_bytes,
        captured_at=now,
        # Written now, from policy. Nothing is stored without a deletion date.
        retention_expires_at=now + timedelta(days=policy.evidence_retention_days),
        metadata_json=dict(metadata or {}),
    )
    db.add(evidence)
    db.flush()

    log_event(
        Event.EVIDENCE_STORED,
        attempt_id=str(attempt.id),
        evidence_id=str(evidence.id),
        kind=kind.value,
        size_bytes=stored.size_bytes,
        retention_days=policy.evidence_retention_days,
        # The path is deliberately absent: it is the only secret protecting the
        # object, and logs are the wrong place for it.
    )
    return CapturedEvidence(evidence=evidence, stored=True)


async def signed_url_for(
    evidence: ProctoringEvidence, *, provider: StorageProvider | None = None
) -> str | None:
    """A short-lived download URL for one artefact.

    Callers must have already established that the requester owns the attempt.
    This function does not check, because an authorization decision hidden
    inside a URL helper is an authorization decision nobody reviews.
    """
    storage = provider or get_storage_provider()
    try:
        return await storage.signed_url(
            bucket=evidence.bucket,
            path=evidence.object_path,
            expires_in=SIGNED_URL_TTL_SECONDS,
        )
    except StorageError:
        # A missing object is normal once retention has purged it.
        return None


def for_attempt(db: Session, attempt: Attempt) -> list[ProctoringEvidence]:
    return list(
        db.scalars(
            select(ProctoringEvidence)
            .where(ProctoringEvidence.attempt_id == attempt.id)
            .order_by(ProctoringEvidence.captured_at)
        )
    )


def expired_evidence(db: Session, *, limit: int = 500) -> list[ProctoringEvidence]:
    """Rows whose retention window has closed.

    Used by the purge job in ``scripts/purge_evidence.py``. Selecting rather
    than deleting in place keeps the object deletion and the row deletion in
    the caller, where a partial failure can be retried.
    """
    return list(
        db.scalars(
            select(ProctoringEvidence)
            .where(ProctoringEvidence.retention_expires_at <= utcnow())
            .order_by(ProctoringEvidence.retention_expires_at)
            .limit(limit)
        )
    )


__all__ = [
    "ALLOWED_CONTENT_TYPES",
    "MAX_CLIP_BYTES",
    "MAX_SNAPSHOT_BYTES",
    "SIGNED_URL_TTL_SECONDS",
    "CapturedEvidence",
    "EvidenceRejected",
    "capture",
    "expired_evidence",
    "for_attempt",
    "max_bytes_for",
    "object_path",
    "signed_url_for",
    "validate_artefact",
]
