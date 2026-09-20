"""Authentication routes.

Supabase Auth issues and refreshes sessions; this API only verifies them and
keeps a local profile row so that roles and ownership have somewhere to live.
"""

from __future__ import annotations

from fastapi import APIRouter, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, Identity
from app.core.errors import AppError
from app.db.models import User, UserRole
from app.schemas.auth import ProfileUpsertRequest, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/profile",
    response_model=UserOut,
    status_code=status.HTTP_200_OK,
    summary="Create or update the caller's profile",
)
def upsert_profile(
    payload: ProfileUpsertRequest,
    identity: Identity,
    db: DbSession,
) -> User:
    """Idempotent upsert, called once after sign-in.

    The email comes from the verified token, never from the body, so a caller
    cannot claim someone else's address. The role is assigned here and is never
    read from the request.
    """
    if not identity.email:
        raise AppError(
            "This account has no verified email address.", code="EMAIL_REQUIRED"
        )
    email = identity.email.strip().lower()

    user = db.scalar(select(User).where(User.supabase_user_id == identity.subject))
    if user is None:
        # An existing row with this email but a different Supabase subject means
        # the address is already claimed; refuse rather than silently rebind it.
        conflicting = db.scalar(select(User).where(User.email == email))
        if conflicting is not None:
            raise AppError(
                "This email is already registered to another account.",
                code="EMAIL_ALREADY_REGISTERED",
                status_code=status.HTTP_409_CONFLICT,
            )
        user = User(
            supabase_user_id=identity.subject,
            email=email,
            name=payload.name,
            role=UserRole.TEACHER,
        )
        db.add(user)
    else:
        user.name = payload.name
        user.email = email

    db.commit()
    db.refresh(user)
    return user


@router.get("/me", response_model=UserOut, summary="Current profile")
def me(user: CurrentUser) -> User:
    return user
