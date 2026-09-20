"""The variant state machine.

    GENERATED --> VALIDATED --> APPROVED --> PUBLISHED
        |             |            |
        +-------------+------------+--> REJECTED

Declared as data so the legal transitions can be read at a glance and tested
exhaustively. Any transition not in the table is refused, which means a new
state cannot quietly become reachable by someone assigning to the column.

Two rules the table encodes and the code enforces:

*Only a human approves.* VALIDATED -> APPROVED is never automatic; passing
deterministic validation is not consent to put a question in front of students.

*PUBLISHED content is frozen.* A published variant may already be on a paper,
so its text can never be edited — editing supersedes it with a new version
instead. It can still be REJECTED, which withdraws it from future papers
without touching the historical ones.
"""

from __future__ import annotations

from app.core.clock import utcnow
from app.core.errors import Conflict
from app.db.models import QuestionVariant, User, VariantStatus

# state -> states reachable from it
LEGAL_TRANSITIONS: dict[VariantStatus, frozenset[VariantStatus]] = {
    VariantStatus.GENERATED: frozenset({VariantStatus.VALIDATED, VariantStatus.REJECTED}),
    VariantStatus.VALIDATED: frozenset({VariantStatus.APPROVED, VariantStatus.REJECTED}),
    VariantStatus.APPROVED: frozenset({VariantStatus.PUBLISHED, VariantStatus.REJECTED}),
    # A published variant's CONTENT is frozen, but a teacher who spots a bad
    # question must be able to pull it. Rejecting removes it from future papers
    # and changes nothing about papers already built, because those are frozen
    # in attempt_questions and reference this row regardless of its status.
    VariantStatus.PUBLISHED: frozenset({VariantStatus.REJECTED}),
    # Rejection is reversible: a teacher who rejects by mistake can send the
    # variant back for review rather than regenerate it.
    VariantStatus.REJECTED: frozenset({VariantStatus.VALIDATED}),
}

# Transitions a teacher may request directly through the API. Everything else
# is driven by the system (validation, publication).
TEACHER_TRANSITIONS: frozenset[tuple[VariantStatus, VariantStatus]] = frozenset(
    {
        (VariantStatus.VALIDATED, VariantStatus.APPROVED),
        (VariantStatus.VALIDATED, VariantStatus.REJECTED),
        (VariantStatus.GENERATED, VariantStatus.REJECTED),
        (VariantStatus.APPROVED, VariantStatus.REJECTED),
        (VariantStatus.PUBLISHED, VariantStatus.REJECTED),
        (VariantStatus.REJECTED, VariantStatus.VALIDATED),
    }
)


class IllegalTransition(Conflict):
    code = "ILLEGAL_VARIANT_TRANSITION"


def can_transition(current: VariantStatus, target: VariantStatus) -> bool:
    return target in LEGAL_TRANSITIONS.get(current, frozenset())


def assert_transition(current: VariantStatus, target: VariantStatus) -> None:
    if not can_transition(current, target):
        raise IllegalTransition(
            f"A variant cannot go from {current.value} to {target.value}.",
            details={
                "from": current.value,
                "to": target.value,
                "allowed": sorted(s.value for s in LEGAL_TRANSITIONS.get(current, frozenset())),
            },
        )


def transition(
    variant: QuestionVariant,
    target: VariantStatus,
    *,
    actor: User | None = None,
    reason: str | None = None,
) -> QuestionVariant:
    """Move a variant to ``target``, or refuse.

    Does not commit: the caller owns the transaction, so a transition and the
    rows it depends on are written together or not at all.
    """
    assert_transition(variant.validation_status, target)

    if target is VariantStatus.APPROVED:
        # Recorded for the audit trail: who let this question through.
        variant.approved_at = utcnow()
        variant.approved_by_id = actor.id if actor is not None else None
        variant.rejection_reason = None
    elif target is VariantStatus.REJECTED:
        variant.rejection_reason = reason
        variant.approved_at = None
        variant.approved_by_id = None
    elif target is VariantStatus.VALIDATED:
        # Sending a rejected variant back for review clears the old reason.
        variant.rejection_reason = None

    variant.validation_status = target
    return variant


def is_in_use(variant: QuestionVariant) -> bool:
    """Whether any attempt has been served this variant.

    A variant in use can never be edited or deleted — the historical paper must
    keep the exact text the student saw.
    """
    return bool(variant.attempt_questions)


__all__ = [
    "IllegalTransition",
    "LEGAL_TRANSITIONS",
    "TEACHER_TRANSITIONS",
    "assert_transition",
    "can_transition",
    "is_in_use",
    "transition",
]
