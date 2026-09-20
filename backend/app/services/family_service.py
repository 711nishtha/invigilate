"""Question family CRUD.

A family is one conceptual question. Its variants are the concrete renderings a
student might be given, so a family's own settings shape what gets generated —
and changing them once variants exist does not retroactively change those
variants.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai.numeric import NumericTemplateError, placeholders_in
from app.core.errors import AppError, Conflict
from app.db.models import QuestionFamily, QuestionType, Quiz, QuizStatus

REQUIRED_NUMERIC_KEYS = ("template", "expression", "parameters")


def validate_generation_config(question_type: QuestionType, config: dict[str, Any]) -> None:
    """Check a family's generation config before it is stored.

    Numeric families carry a template the backend will execute, so the shape is
    checked at authoring time rather than at generation time — a teacher should
    learn about a broken template while writing it, not an hour later.
    """
    if question_type is not QuestionType.NUMERICAL:
        return

    missing = [key for key in REQUIRED_NUMERIC_KEYS if not config.get(key)]
    if missing:
        raise NumericTemplateError(
            "A numerical question needs a template, an expression and parameters.",
            details={"missing": missing},
        )

    parameters = config["parameters"]
    if not isinstance(parameters, dict) or not parameters:
        raise NumericTemplateError("'parameters' must be a non-empty object.")

    declared = set(parameters)
    referenced = set(placeholders_in(str(config["template"])))
    unknown = referenced - declared
    if unknown:
        raise NumericTemplateError(
            "The template references parameters that are not declared.",
            details={"unknown": sorted(unknown), "declared": sorted(declared)},
        )
    unused = declared - referenced
    if unused:
        raise NumericTemplateError(
            "Every declared parameter must appear in the template.",
            details={"unused": sorted(unused)},
        )


def _assert_quiz_editable(quiz: Quiz) -> None:
    """Questions cannot be added or removed from a live exam."""
    if quiz.status is QuizStatus.PUBLISHED:
        raise Conflict(
            "Unpublish the quiz before changing its questions.",
            code="QUIZ_PUBLISHED_IMMUTABLE",
        )
    if quiz.status in (QuizStatus.CLOSED, QuizStatus.ARCHIVED):
        raise Conflict(
            f"A {quiz.status.value.lower()} quiz cannot be changed.", code="QUIZ_NOT_EDITABLE"
        )


def create_family(db: Session, quiz: Quiz, data: dict[str, Any]) -> QuestionFamily:
    _assert_quiz_editable(quiz)

    config = data.get("generation_config_json") or {}
    validate_generation_config(data["question_type"], config)

    if data.get("position") is None:
        # Append: one past the current highest position.
        highest = db.scalar(
            select(func.max(QuestionFamily.position)).where(QuestionFamily.quiz_id == quiz.id)
        )
        data["position"] = 0 if highest is None else int(highest) + 1

    family = QuestionFamily(quiz_id=quiz.id, **data)
    db.add(family)
    db.commit()
    db.refresh(family)
    return family


def update_family(
    db: Session, family: QuestionFamily, changes: dict[str, Any]
) -> QuestionFamily:
    """Edit a family's own fields.

    Editing a family never rewrites existing variants. Those were generated
    from the settings in force at the time, and some may already be on a paper;
    the new settings apply to the next generation run.
    """
    _assert_quiz_editable(family.quiz)

    question_type = changes.get("question_type", family.question_type)
    config = changes.get("generation_config_json", family.generation_config_json) or {}
    validate_generation_config(question_type, config)

    if "question_type" in changes and changes["question_type"] != family.question_type:
        if family.variants:
            raise Conflict(
                "Changing the question type would invalidate the variants already "
                "generated for it. Delete them first.",
                code="FAMILY_HAS_VARIANTS",
                details={"variant_count": len(family.variants)},
            )

    for field, value in changes.items():
        setattr(family, field, value)
    db.commit()
    db.refresh(family)
    return family


def delete_family(db: Session, family: QuestionFamily) -> None:
    """Delete a family and its variants.

    Refused when any variant has been served: the historical paper references
    it, and ``ON DELETE RESTRICT`` on attempt_questions.variant_id would refuse
    anyway. Checking here turns a database error into an explanation.
    """
    _assert_quiz_editable(family.quiz)

    in_use = [v for v in family.variants if v.attempt_questions]
    if in_use:
        raise Conflict(
            "This question has been used in a student's exam and cannot be deleted.",
            code="FAMILY_IN_USE",
            details={"variant_ids": [str(v.id) for v in in_use]},
        )

    db.delete(family)
    db.commit()


def reorder_families(db: Session, quiz: Quiz, ordered_ids: list[str]) -> list[QuestionFamily]:
    """Set family positions from an explicit ordering."""
    _assert_quiz_editable(quiz)

    by_id = {str(family.id): family for family in quiz.question_families}
    if set(ordered_ids) != set(by_id):
        raise AppError(
            "The ordering must list every question in this quiz exactly once.",
            code="INCOMPLETE_ORDERING",
        )

    for position, family_id in enumerate(ordered_ids):
        by_id[family_id].position = position
    db.commit()
    return sorted(by_id.values(), key=lambda f: f.position)


__all__ = [
    "create_family",
    "delete_family",
    "reorder_families",
    "update_family",
    "validate_generation_config",
]
