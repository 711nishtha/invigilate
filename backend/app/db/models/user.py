"""User model.

The role stored here is the only authoritative role in the system. A JWT says
who the caller is; this row says what they may do.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, created_at_column, uuid_pk
from app.db.models.enums import UserRole
from app.db.types import GUID, pg_enum

if TYPE_CHECKING:
    from app.db.models.quiz import Quiz


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = uuid_pk()
    supabase_user_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # Normalized to lowercase on write so uniqueness is case-insensitive
    # without depending on the citext extension.
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    role: Mapped[UserRole] = mapped_column(
        pg_enum(UserRole, "user_role"),
        nullable=False,
        default=UserRole.TEACHER,
        server_default=UserRole.TEACHER.value,
    )
    created_at: Mapped[datetime] = created_at_column()

    quizzes: Mapped[list["Quiz"]] = relationship(
        back_populates="owner", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_users_supabase_user_id", "supabase_user_id"),)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<User {self.email} role={self.role.value}>"


__all__ = ["User", "GUID"]
