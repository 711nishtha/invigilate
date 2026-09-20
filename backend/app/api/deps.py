"""Shared FastAPI dependencies: authentication and authorization.

These are the only place a request is turned into a trusted identity.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotAuthenticated, PermissionDenied
from app.core.security import SupabaseIdentity, verify_supabase_jwt
from app.db.models import User, UserRole
from app.db.session import get_db

DbSession = Annotated[Session, Depends(get_db)]


def _bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise NotAuthenticated("Authorization header is missing.")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise NotAuthenticated("Expected an 'Authorization: Bearer <token>' header.")
    return token.strip()


def get_identity(
    authorization: Annotated[str | None, Header()] = None,
) -> SupabaseIdentity:
    """Verified Supabase JWT claims. Proves identity only, never authority."""
    return verify_supabase_jwt(_bearer_token(authorization))


Identity = Annotated[SupabaseIdentity, Depends(get_identity)]


def get_current_user(identity: Identity, db: DbSession) -> User:
    """The caller's database row.

    The row, not the token, carries the role. A user who authenticates but has
    no profile yet must call POST /api/auth/profile first.
    """
    user = db.scalar(select(User).where(User.supabase_user_id == identity.subject))
    if user is None:
        raise NotAuthenticated(
            "No profile exists for this account. Call POST /api/auth/profile first.",
            code="PROFILE_NOT_FOUND",
        )
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def get_current_teacher(user: CurrentUser) -> User:
    """Guard for teacher-only routes."""
    if user.role not in (UserRole.TEACHER, UserRole.ADMIN):
        raise PermissionDenied("This endpoint requires a teacher account.")
    return user


CurrentTeacher = Annotated[User, Depends(get_current_teacher)]


def get_attempt_token(
    x_attempt_token: Annotated[str | None, Header()] = None,
) -> str:
    """The student's per-attempt bearer token.

    Scoped to exactly one attempt: it grants no access to any other attempt,
    quiz, or teacher route.
    """
    if not x_attempt_token:
        raise NotAuthenticated("Missing X-Attempt-Token header.", code="MISSING_ATTEMPT_TOKEN")
    return x_attempt_token.strip()


AttemptToken = Annotated[str, Depends(get_attempt_token)]
