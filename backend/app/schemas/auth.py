"""Authentication schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import EmailStr, Field

from app.db.models.enums import UserRole
from app.schemas.common import APIModel, StrictModel


class ProfileUpsertRequest(StrictModel):
    """Body of POST /api/auth/profile.

    There is deliberately no ``role`` field. Role is assigned by the server;
    accepting one here would be a privilege-escalation endpoint. ``extra="forbid"``
    means sending one is a 422, not a silent no-op.
    """

    name: str = Field(min_length=1, max_length=200)


class UserOut(APIModel):
    id: uuid.UUID
    name: str
    email: EmailStr
    role: UserRole
    created_at: datetime
