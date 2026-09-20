"""Student identity for the one-attempt rule.

The MVP identifies a student by the email they type on the pre-exam screen,
enforced by ``UNIQUE (quiz_id, student_email)``. That is a weak identity claim
and is documented as such in docs/security.md.

This module exists so that a stronger mechanism can replace it without
redesigning ``Attempt``. Every caller goes through :func:`resolve_identity` and
reads ``dedupe_key``; nothing outside this module decides what "the same
student" means. Adding a mechanism means adding a resolver here and a migration
that widens the unique constraint's column — not touching attempt creation,
grading, or any route.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.core.errors import AppError
from app.db.models import Quiz, User


class IdentityMechanism(str, Enum):
    """How a student's identity was established.

    Only ``SELF_DECLARED_EMAIL`` is implemented. The others are named here
    because the value is what a future migration would backfill, and naming
    them now keeps the column's meaning stable.
    """

    SELF_DECLARED_EMAIL = "SELF_DECLARED_EMAIL"
    SUPABASE_ACCOUNT = "SUPABASE_ACCOUNT"  # signed-in student
    ROSTER_ENROLMENT = "ROSTER_ENROLMENT"  # teacher-uploaded roster
    LMS_LAUNCH = "LMS_LAUNCH"  # LTI launch from an LMS


@dataclass(frozen=True)
class StudentIdentity:
    """A resolved student identity.

    ``dedupe_key`` is the value the one-attempt rule keys on. Today it equals
    the normalized email, which is why it maps onto ``attempts.student_email``
    unchanged; a future mechanism can make it a roster id or a Supabase user id
    without any caller noticing.
    """

    display_name: str
    email: str
    dedupe_key: str
    mechanism: IdentityMechanism
    user_id: str | None = None

    @property
    def is_verified(self) -> bool:
        """Whether the platform, rather than the student, asserted this identity.

        Self-declared email is not verified: anyone can type anyone's address.
        Callers that need a trustworthy identity must check this rather than
        assuming a resolved identity is a proven one.
        """
        return self.mechanism is not IdentityMechanism.SELF_DECLARED_EMAIL


def normalize_email(email: str) -> str:
    """Casefold and trim so that identity comparison is case-insensitive.

    ``attempts.student_email`` is a plain varchar rather than citext, so this
    normalization is what makes ``UNIQUE (quiz_id, student_email)`` actually
    catch "Student@Uni.edu" after "student@uni.edu".
    """
    normalized = email.strip().lower()
    if not normalized or "@" not in normalized:
        raise AppError("A valid email address is required.", code="INVALID_EMAIL")
    return normalized


def resolve_identity(
    *,
    quiz: Quiz,
    student_name: str,
    student_email: str,
    signed_in_user: User | None = None,
) -> StudentIdentity:
    """Resolve the identity an attempt will be created under.

    A signed-in student is preferred over a typed email because the account is
    a stronger claim, and because it survives a student typing a different
    address on a second device to get a second attempt.
    """
    name = student_name.strip()
    if not name:
        raise AppError("A student name is required.", code="INVALID_STUDENT_NAME")

    if signed_in_user is not None:
        email = normalize_email(signed_in_user.email)
        return StudentIdentity(
            display_name=signed_in_user.name or name,
            email=email,
            # Keyed on the account, not on whatever was typed in the form.
            dedupe_key=email,
            mechanism=IdentityMechanism.SUPABASE_ACCOUNT,
            user_id=str(signed_in_user.id),
        )

    email = normalize_email(student_email)
    return StudentIdentity(
        display_name=name,
        email=email,
        dedupe_key=email,
        mechanism=IdentityMechanism.SELF_DECLARED_EMAIL,
    )


__all__ = [
    "IdentityMechanism",
    "StudentIdentity",
    "normalize_email",
    "resolve_identity",
]
