"""SQLAlchemy models.

Importing this package registers every table on ``Base.metadata``, which is
what Alembic autogenerate and the test fixtures rely on.
"""

from app.db.base import Base
from app.db.models.attempt import Answer, Attempt, AttemptQuestion
from app.db.models.enums import (
    AttemptStatus,
    GenerationStatus,
    IntegrityAction,
    IntegrityEventType,
    IntegrityStatus,
    QuestionType,
    QuizStatus,
    SecurityLevel,
    UserRole,
    VariantStatus,
)
from app.db.models.integrity import IntegrityEvent
from app.db.models.question import QuestionFamily, QuestionVariant
from app.db.models.quiz import Quiz
from app.db.models.user import User

__all__ = [
    "Answer",
    "Attempt",
    "AttemptQuestion",
    "AttemptStatus",
    "Base",
    "GenerationStatus",
    "IntegrityAction",
    "IntegrityEvent",
    "IntegrityEventType",
    "IntegrityStatus",
    "QuestionFamily",
    "QuestionType",
    "QuestionVariant",
    "Quiz",
    "QuizStatus",
    "SecurityLevel",
    "User",
    "UserRole",
    "VariantStatus",
]
