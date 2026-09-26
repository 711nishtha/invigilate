"""Teacher proctoring review.

Two endpoints: read one attempt's proctoring record, and record a decision
about it.

The framing of the read endpoint is deliberate. It returns the timeline, the
evidence and a summary that says what was observed and how much of it the
platform can vouch for — including, prominently, the caveats. It does not
return a suspicion score, a ranking, or a probability that a student cheated,
because the platform is not in a position to produce any of those honestly.

The write endpoint records a *person's* judgement, attributed and timestamped.
``ProctoringReview`` has no "cheating" value, so this endpoint cannot be used to
record that finding. Academic misconduct is a process that happens outside a
quiz platform; a dropdown here would misrepresent both the evidence and the
process.
"""

# NOTE: no `from __future__ import annotations`; see student.py.
from fastapi import APIRouter

from app.api.deps import CurrentTeacher, DbSession
from app.core.clock import ensure_aware, utcnow
from app.db.models import Attempt, Quiz
from app.lockdown import get_lockdown_provider
from app.schemas.proctoring import (
    EvidenceOut,
    ProctoringDetailOut,
    ProctoringEventOut,
    ProctoringReviewRequest,
    ProctoringSummaryOut,
)
from app.services import evidence_service, proctoring_service
from app.services.ownership import parse_uuid
from app.core.errors import NotFound
from sqlalchemy import select
from sqlalchemy.orm import selectinload

router = APIRouter(prefix="/attempts", tags=["proctoring"])


def _owned_attempt(db: DbSession, attempt_id: str, teacher) -> tuple[Attempt, Quiz]:  # type: ignore[no-untyped-def]
    """Resolve an attempt the caller owns, or 404.

    Ownership is a predicate inside the query — joined to ``quizzes`` on
    ``owner_id`` — rather than a check performed after loading the row. A
    teacher asking for somebody else's attempt gets the same 404 as one asking
    for an attempt that does not exist, so the endpoint reveals nothing about
    another teacher's exams.
    """
    attempt = db.scalar(
        select(Attempt)
        .join(Quiz, Quiz.id == Attempt.quiz_id)
        .where(Attempt.id == parse_uuid(attempt_id, what="attempt"), Quiz.owner_id == teacher.id)
        .options(selectinload(Attempt.integrity_events))
    )
    if attempt is None:
        raise NotFound("No such attempt.", code="ATTEMPT_NOT_FOUND")
    quiz = db.get(Quiz, attempt.quiz_id)
    assert quiz is not None  # guaranteed by the join above
    return attempt, quiz


@router.get("/{attempt_id}/proctoring", response_model=ProctoringDetailOut)
async def get_proctoring_detail(
    attempt_id: str, teacher: CurrentTeacher, db: DbSession
) -> ProctoringDetailOut:
    """The full proctoring record for one attempt.

    Evidence download URLs are minted here, after the ownership check above, and
    expire in five minutes. The stored object path is never returned: it is the
    only thing protecting the object, and a URL that expires is a smaller
    liability than a path that does not.
    """
    attempt, quiz = _owned_attempt(db, attempt_id, teacher)

    artefacts = evidence_service.for_attempt(db, attempt)
    summary = proctoring_service.summarize(
        db, attempt, quiz, evidence_count=len(artefacts)
    )

    evidence_out = []
    for artefact in artefacts:
        evidence_out.append(
            EvidenceOut(
                id=artefact.id,
                kind=artefact.kind,
                captured_at=ensure_aware(artefact.captured_at),
                content_type=artefact.content_type,
                size_bytes=artefact.size_bytes,
                retention_expires_at=ensure_aware(artefact.retention_expires_at),
                download_url=await evidence_service.signed_url_for(artefact),
                metadata=artefact.metadata_json or {},
            )
        )

    precheck = None
    if attempt.precheck_session_id is not None:
        from app.db.models import PrecheckSession

        session = db.get(PrecheckSession, attempt.precheck_session_id)
        if session is not None:
            precheck = {
                "status": session.status.value,
                "mode": session.proctoring_mode.value,
                "authorized_at": (
                    ensure_aware(session.authorized_at).isoformat()
                    if session.authorized_at
                    else None
                ),
                "consent_accepted_at": (
                    ensure_aware(session.consent_accepted_at).isoformat()
                    if session.consent_accepted_at
                    else None
                ),
                # The server's own record of what it decided and why.
                "verdict": session.verdict_json or {},
                # The browser's claims, labelled as claims.
                "reported_capabilities": session.capabilities_json or {},
                "reported_observations": session.observations_json or {},
            }

    return ProctoringDetailOut(
        attempt_id=attempt.id,
        student_name=attempt.student_name,
        student_email=attempt.student_email,
        summary=ProctoringSummaryOut(**summary.as_dict()),
        # Shown in the UI so a teacher reading signals also reads what the
        # monitoring could not see.
        monitoring_capabilities=get_lockdown_provider(
            attempt.proctoring_mode
        ).capabilities().as_dict(),
        precheck=precheck,
        events=[
            ProctoringEventOut(
                id=event.id,
                event_type=event.event_type.value,
                severity=event.severity,
                occurred_at=ensure_aware(event.occurred_at),
                action_taken=event.action_taken.value,
                metadata=event.metadata_json or {},
            )
            for event in attempt.integrity_events
        ],
        evidence=evidence_out,
        last_heartbeat_at=(
            ensure_aware(attempt.last_heartbeat_at) if attempt.last_heartbeat_at else None
        ),
        server_time=utcnow(),
    )


@router.post("/{attempt_id}/proctoring/review", response_model=ProctoringSummaryOut)
def record_proctoring_review(
    attempt_id: str,
    payload: ProctoringReviewRequest,
    teacher: CurrentTeacher,
    db: DbSession,
) -> ProctoringSummaryOut:
    """Record a teacher's conclusion about the proctoring timeline.

    Changes no mark. The academic result and the proctoring review are separate
    fields on the attempt, and this writes only the second — a proctoring
    concern must never silently alter a score.
    """
    attempt, quiz = _owned_attempt(db, attempt_id, teacher)
    attempt = proctoring_service.record_review(
        db,
        attempt,
        decision=payload.decision,
        note=payload.note,
        reviewer_id=teacher.id,
    )
    artefacts = evidence_service.for_attempt(db, attempt)
    summary = proctoring_service.summarize(
        db, attempt, quiz, evidence_count=len(artefacts)
    )
    return ProctoringSummaryOut(**summary.as_dict())


__all__ = ["router"]
