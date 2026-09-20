"""Variant selection primitives.

Phase 2 deliberately stops short of creating attempts. What it does provide is
the part the assignment engine will be built on: a definition of which variants
are eligible, and a deterministic, seeded way to choose among them.

Two design points that matter for Phase 4:

*Selection is family-first, not row-first.* "Pick N random rows from
question_variants" would give one student two variants of the same question and
another none. The engine picks families, then one variant per family, which is
what makes papers differ while staying equivalent assessments.

*Selection is reproducible.* Everything here is driven by an explicit seed, so
an attempt's paper can be re-derived and explained months later from the seed
stored on the attempt row. Nothing calls the global RNG.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import QuestionFamily, QuestionVariant, Quiz, VariantStatus


@dataclass(frozen=True)
class SelectionCriteria:
    """Filters the assignment engine may apply when building a paper.

    Present now so the eventual engine has a stable vocabulary; the fields map
    onto the axes a teacher authors against (topic, difficulty, objective).
    """

    question_count: int
    topics: tuple[str, ...] = ()
    difficulties: tuple[str, ...] = ()
    learning_objectives: tuple[str, ...] = ()

    @classmethod
    def for_quiz(cls, quiz: Quiz) -> SelectionCriteria:
        coverage = quiz.coverage_config_json or {}
        return cls(
            question_count=quiz.question_count,
            topics=tuple(coverage.get("required_topics", ())),
            difficulties=tuple(coverage.get("required_difficulties", ())),
            learning_objectives=tuple(coverage.get("required_objectives", ())),
        )


@dataclass(frozen=True)
class FamilyPool:
    """One family and the variants a student could be given from it."""

    family_id: uuid.UUID
    position: int
    topic: str | None
    difficulty: str | None
    learning_objective: str | None
    variant_ids: tuple[uuid.UUID, ...]

    @property
    def size(self) -> int:
        return len(self.variant_ids)


def assignable_variants_query(family_ids: list[uuid.UUID]):  # type: ignore[no-untyped-def]
    """The canonical eligibility filter.

    Exactly one definition of "may be served to a student", used by the pool
    builder and by publish validation, so the two can never disagree:
    approved or published, and not superseded by a newer version.
    """
    return select(QuestionVariant).where(
        QuestionVariant.family_id.in_(family_ids),
        QuestionVariant.validation_status.in_(
            [VariantStatus.APPROVED, VariantStatus.PUBLISHED]
        ),
        QuestionVariant.superseded_by_id.is_(None),
    )


def build_pools(db: Session, quiz: Quiz) -> list[FamilyPool]:
    """The assignable pool for each family, in authoring order."""
    families = list(
        db.scalars(
            select(QuestionFamily)
            .where(QuestionFamily.quiz_id == quiz.id)
            .order_by(QuestionFamily.position)
        )
    )
    if not families:
        return []

    variants = list(db.scalars(assignable_variants_query([f.id for f in families])))
    by_family: dict[uuid.UUID, list[QuestionVariant]] = {}
    for variant in variants:
        by_family.setdefault(variant.family_id, []).append(variant)

    pools: list[FamilyPool] = []
    for family in families:
        candidates = by_family.get(family.id, [])
        if not candidates:
            continue
        pools.append(
            FamilyPool(
                family_id=family.id,
                position=family.position,
                topic=family.topic,
                difficulty=family.difficulty,
                learning_objective=family.learning_objective,
                # Sorted so the pool order does not depend on how the database
                # happened to return rows; the seed alone decides the draw.
                variant_ids=tuple(sorted((v.id for v in candidates), key=str)),
            )
        )
    return pools


def filter_pools(pools: list[FamilyPool], criteria: SelectionCriteria) -> list[FamilyPool]:
    """Narrow pools to those matching the criteria.

    An empty filter matches everything, so a quiz with no coverage config
    behaves exactly as before.
    """

    def matches(pool: FamilyPool) -> bool:
        if criteria.topics and (pool.topic or "").casefold() not in {
            t.casefold() for t in criteria.topics
        }:
            return False
        if criteria.difficulties and (pool.difficulty or "").casefold() not in {
            d.casefold() for d in criteria.difficulties
        }:
            return False
        if criteria.learning_objectives and (pool.learning_objective or "").casefold() not in {
            o.casefold() for o in criteria.learning_objectives
        }:
            return False
        return True

    return [pool for pool in pools if matches(pool)]


def plan_paper(
    pools: list[FamilyPool], *, seed: int, question_count: int
) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """Choose ``(family_id, variant_id)`` pairs for one student, deterministically.

    This is the selection algorithm Phase 4's assignment engine will call. It
    does not create an attempt, write any row, or shuffle options — those are
    Phase 4's job. Isolating it here means the randomization properties can be
    tested on their own.

    Same seed and same pools always produce the same paper.
    """
    if question_count <= 0:
        return []

    rng = random.Random(seed)
    # Sample families first, so a student never receives two variants of the
    # same underlying question.
    ordered = sorted(pools, key=lambda pool: pool.position)
    chosen_families = (
        ordered
        if question_count >= len(ordered)
        else rng.sample(ordered, question_count)
    )

    plan: list[tuple[uuid.UUID, uuid.UUID]] = []
    for pool in sorted(chosen_families, key=lambda pool: pool.position):
        plan.append((pool.family_id, rng.choice(list(pool.variant_ids))))
    return plan


def distinct_paper_estimate(pools: list[FamilyPool], question_count: int) -> int:
    """How many distinct papers the current pool can produce.

    Uses the largest pools, which is the count a teacher cares about. Capped so
    an implausibly large product stays readable.
    """
    if not pools:
        return 0
    sizes = sorted((pool.size for pool in pools), reverse=True)[:question_count]
    product = 1
    for size in sizes:
        product *= size
        if product > 10**12:
            return 10**12
    return product


__all__ = [
    "FamilyPool",
    "SelectionCriteria",
    "assignable_variants_query",
    "build_pools",
    "distinct_paper_estimate",
    "filter_pools",
    "plan_paper",
]
