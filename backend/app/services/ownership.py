"""Teacher ownership authorization.

The teacher-side counterpart to ``attempt_access``: the single gate that turns
a request for a quiz, family or variant into a resource the caller is allowed
to touch. Routes call these resolvers instead of querying the tables
themselves, so ownership holds for every endpoint by construction.

Two rules, both carried over from Phase 1:

*Ownership is a WHERE clause, not a post-fetch check.* A mis-scoped query
returns nothing rather than a row somebody then forgets to compare.

*Every failure is 404.* Someone else's quiz is indistinguishable from a quiz
that does not exist, so ids cannot be probed.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.errors import NotFound
from app.db.models import QuestionFamily, QuestionVariant, Quiz, User, UserRole


def _not_found(what: str) -> NotFound:
    return NotFound(f"No such {what}.", code=f"{what.upper().replace(' ', '_')}_NOT_FOUND")


def parse_uuid(value: str | uuid.UUID, *, what: str) -> uuid.UUID:
    """Parse an identifier, treating a malformed one as an authorization failure.

    A 422 here would tell a caller that the id was well-formed but unknown,
    which is a distinction worth denying them.
    """
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise _not_found(what) from None


def _owner_scope(user: User):  # type: ignore[no-untyped-def]
    """The ownership predicate for this caller.

    An ADMIN sees every quiz; a TEACHER sees only their own. Expressed as a
    clause so it composes into the query rather than being applied afterwards.
    """
    if user.role is UserRole.ADMIN:
        return Quiz.id.is_not(None)
    return Quiz.owner_id == user.id


def get_owned_quiz(
    db: Session,
    quiz_id: str | uuid.UUID,
    user: User,
    *,
    with_families: bool = False,
) -> Quiz:
    """Load a quiz the caller owns, or raise 404."""
    statement = select(Quiz).where(Quiz.id == parse_uuid(quiz_id, what="quiz"), _owner_scope(user))
    if with_families:
        statement = statement.options(
            selectinload(Quiz.question_families).selectinload(QuestionFamily.variants)
        )
    quiz = db.scalar(statement)
    if quiz is None:
        raise _not_found("quiz")
    return quiz


def get_owned_family(
    db: Session,
    family_id: str | uuid.UUID,
    user: User,
    *,
    with_variants: bool = False,
) -> QuestionFamily:
    """Load a question family whose quiz the caller owns.

    The join to ``quizzes`` is what enforces ownership: a family belonging to
    another teacher's quiz simply does not match.
    """
    statement = (
        select(QuestionFamily)
        .join(Quiz, QuestionFamily.quiz_id == Quiz.id)
        .where(QuestionFamily.id == parse_uuid(family_id, what="question family"), _owner_scope(user))
    )
    if with_variants:
        statement = statement.options(selectinload(QuestionFamily.variants))
    family = db.scalar(statement)
    if family is None:
        raise _not_found("question family")
    return family


def get_owned_variant(
    db: Session, variant_id: str | uuid.UUID, user: User
) -> QuestionVariant:
    """Load a variant whose family's quiz the caller owns.

    Two joins deep, still a single query and still a WHERE clause.
    """
    variant = db.scalar(
        select(QuestionVariant)
        .join(QuestionFamily, QuestionVariant.family_id == QuestionFamily.id)
        .join(Quiz, QuestionFamily.quiz_id == Quiz.id)
        .where(QuestionVariant.id == parse_uuid(variant_id, what="variant"), _owner_scope(user))
        .options(selectinload(QuestionVariant.family))
    )
    if variant is None:
        raise _not_found("variant")
    return variant


__all__ = ["get_owned_family", "get_owned_quiz", "get_owned_variant", "parse_uuid"]
