"""Question family and variant routes.

Ownership for every handler comes from ``services.ownership``: a family is
reachable only through its quiz's owner, and a variant only through its
family's quiz's owner. Both are single queries with the ownership predicate in
the WHERE clause.
"""

# NOTE: no `from __future__ import annotations` here on purpose. Rate-limited
# handlers are wrapped by slowapi, and FastAPI then resolves their annotations
# against the wrapper's module globals, where our dependency aliases do not
# exist. Real annotation objects sidestep that entirely.
from fastapi import APIRouter, Request, Response, status

from app.api.deps import CurrentTeacher, DbSession
from app.core.config import settings
from app.core.rate_limit import limiter
from app.db.models import QuestionFamily, QuestionVariant, VariantStatus
from app.schemas.question import (
    FamilyCreateRequest,
    FamilyOut,
    FamilyReorderRequest,
    FamilyUpdateRequest,
    GenerateVariantsRequest,
    GenerationResultOut,
    VariantCountsOut,
    VariantCreateRequest,
    VariantOut,
    VariantRejectRequest,
    VariantUpdateRequest,
)
from app.services import family_service, variant_service
from app.services.ownership import get_owned_family, get_owned_quiz, get_owned_variant

quiz_scoped = APIRouter(prefix="/quizzes", tags=["questions"])
family_scoped = APIRouter(prefix="/questions", tags=["questions"])
variant_scoped = APIRouter(prefix="/variants", tags=["variants"])


def _counts(family: QuestionFamily) -> VariantCountsOut:
    tally = {status.value.lower(): 0 for status in VariantStatus}
    assignable = 0
    for variant in family.variants:
        tally[variant.validation_status.value.lower()] += 1
        if variant.is_assignable:
            assignable += 1
    return VariantCountsOut(**tally, assignable=assignable)


def _family_out(family: QuestionFamily) -> FamilyOut:
    return FamilyOut(
        **{
            field: getattr(family, field)
            for field in FamilyOut.model_fields
            if field != "variant_counts"
        },
        variant_counts=_counts(family),
    )


def _variant_out(variant: QuestionVariant) -> VariantOut:
    return VariantOut(
        **{
            field: getattr(variant, field)
            for field in VariantOut.model_fields
            if field not in ("is_assignable", "in_use")
        },
        is_assignable=variant.is_assignable,
        in_use=bool(variant.attempt_questions),
    )


# --- Question families -----------------------------------------------------


@quiz_scoped.post(
    "/{quiz_id}/questions", response_model=FamilyOut, status_code=status.HTTP_201_CREATED
)
def create_family(
    quiz_id: str, payload: FamilyCreateRequest, teacher: CurrentTeacher, db: DbSession
) -> FamilyOut:
    quiz = get_owned_quiz(db, quiz_id, teacher)
    family = family_service.create_family(db, quiz, payload.model_dump())
    return _family_out(family)


@quiz_scoped.get("/{quiz_id}/questions", response_model=list[FamilyOut])
def list_families(quiz_id: str, teacher: CurrentTeacher, db: DbSession) -> list[FamilyOut]:
    quiz = get_owned_quiz(db, quiz_id, teacher, with_families=True)
    return [_family_out(family) for family in quiz.question_families]


@quiz_scoped.post("/{quiz_id}/questions/reorder", response_model=list[FamilyOut])
def reorder_families(
    quiz_id: str, payload: FamilyReorderRequest, teacher: CurrentTeacher, db: DbSession
) -> list[FamilyOut]:
    quiz = get_owned_quiz(db, quiz_id, teacher, with_families=True)
    families = family_service.reorder_families(
        db, quiz, [str(fid) for fid in payload.ordered_family_ids]
    )
    return [_family_out(family) for family in families]


@family_scoped.get("/{family_id}", response_model=FamilyOut)
def get_family(family_id: str, teacher: CurrentTeacher, db: DbSession) -> FamilyOut:
    return _family_out(get_owned_family(db, family_id, teacher, with_variants=True))


@family_scoped.patch("/{family_id}", response_model=FamilyOut)
def update_family(
    family_id: str, payload: FamilyUpdateRequest, teacher: CurrentTeacher, db: DbSession
) -> FamilyOut:
    family = get_owned_family(db, family_id, teacher, with_variants=True)
    return _family_out(family_service.update_family(db, family, payload.changes()))


@family_scoped.delete("/{family_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_family(family_id: str, teacher: CurrentTeacher, db: DbSession) -> None:
    family = get_owned_family(db, family_id, teacher, with_variants=True)
    family_service.delete_family(db, family)


# --- Variants --------------------------------------------------------------


@family_scoped.get("/{family_id}/variants", response_model=list[VariantOut])
def list_variants(family_id: str, teacher: CurrentTeacher, db: DbSession) -> list[VariantOut]:
    family = get_owned_family(db, family_id, teacher, with_variants=True)
    variants = variant_service.variants_for_family(db, family.id)
    return [_variant_out(variant) for variant in variants]


@family_scoped.post(
    "/{family_id}/variants", response_model=VariantOut, status_code=status.HTTP_201_CREATED
)
def create_variant(
    family_id: str, payload: VariantCreateRequest, teacher: CurrentTeacher, db: DbSession
) -> VariantOut:
    family = get_owned_family(db, family_id, teacher, with_variants=True)
    variant = variant_service.create_manual_variant(db, family, payload.model_dump())
    return _variant_out(variant)


@family_scoped.post("/{family_id}/generate-variants", response_model=GenerationResultOut)
@limiter.limit(settings.rate_limit_generation)
async def generate_variants(
    request: Request,
    response: Response,  # slowapi injects rate-limit headers into this
    family_id: str,
    payload: GenerateVariantsRequest,
    teacher: CurrentTeacher,
    db: DbSession,
) -> GenerationResultOut:
    """Generate candidate variants for review.

    Runs during quiz preparation only. A provider failure returns a 200 with
    ``error_code`` set rather than an exception: the family is untouched, the
    teacher is told what happened, and nothing about an already-published quiz
    is affected.
    """
    family = get_owned_family(db, family_id, teacher, with_variants=True)
    outcome = await variant_service.generate_variants(
        db, family, count=payload.count, difficulty=payload.difficulty
    )
    return GenerationResultOut(
        family_id=outcome.family_id,
        requested=outcome.requested,
        accepted=outcome.accepted_count,
        rejected=[
            {"index": r.index, "reasons": r.reasons} for r in outcome.rejected  # type: ignore[list-item]
        ],
        provider=outcome.provider,
        model=outcome.model,
        prompt_version=outcome.prompt_version,
        variants=[_variant_out(v) for v in outcome.accepted],
        error_code=outcome.error_code,
        error_message=outcome.error_message,
    )


@variant_scoped.get("/{variant_id}", response_model=VariantOut)
def get_variant(variant_id: str, teacher: CurrentTeacher, db: DbSession) -> VariantOut:
    return _variant_out(get_owned_variant(db, variant_id, teacher))


@variant_scoped.patch("/{variant_id}", response_model=VariantOut)
def update_variant(
    variant_id: str, payload: VariantUpdateRequest, teacher: CurrentTeacher, db: DbSession
) -> VariantOut:
    """Edit a variant.

    If it has been approved, published or served, this returns a **new**
    variant: the original is superseded rather than rewritten, so historical
    papers keep the exact question the student saw.
    """
    variant = get_owned_variant(db, variant_id, teacher)
    updated = variant_service.edit_variant(db, variant, payload.changes(), teacher)
    return _variant_out(updated)


@variant_scoped.post("/{variant_id}/approve", response_model=VariantOut)
def approve_variant(variant_id: str, teacher: CurrentTeacher, db: DbSession) -> VariantOut:
    variant = get_owned_variant(db, variant_id, teacher)
    return _variant_out(variant_service.approve_variant(db, variant, teacher))


@variant_scoped.post("/{variant_id}/reject", response_model=VariantOut)
def reject_variant(
    variant_id: str, payload: VariantRejectRequest, teacher: CurrentTeacher, db: DbSession
) -> VariantOut:
    variant = get_owned_variant(db, variant_id, teacher)
    return _variant_out(variant_service.reject_variant(db, variant, teacher, payload.reason))


@variant_scoped.post("/{variant_id}/reinstate", response_model=VariantOut)
def reinstate_variant(variant_id: str, teacher: CurrentTeacher, db: DbSession) -> VariantOut:
    """Send a rejected variant back for review."""
    variant = get_owned_variant(db, variant_id, teacher)
    return _variant_out(variant_service.reinstate_variant(db, variant, teacher))


@variant_scoped.delete("/{variant_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_variant(variant_id: str, teacher: CurrentTeacher, db: DbSession) -> None:
    variant = get_owned_variant(db, variant_id, teacher)
    variant_service.delete_variant(db, variant)


__all__ = ["family_scoped", "quiz_scoped", "variant_scoped"]
