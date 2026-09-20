"""Declarative base and shared column helpers."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base for every ORM model.

    Naming conventions are set explicitly so Alembic autogenerate produces
    stable, reversible names for constraints and indexes.
    """

    from sqlalchemy import MetaData

    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_N_name)s",
            "uq": "uq_%(table_name)s_%(column_0_N_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


def uuid_pk() -> Mapped[uuid.UUID]:
    """UUID primary key generated in Python.

    Application-side generation keeps inserts free of a database extension and
    lets services build related rows before a flush.
    """
    from app.db.types import GUID

    return mapped_column(GUID, primary_key=True, default=uuid.uuid4)


def created_at_column() -> Mapped[datetime]:
    """Creation timestamp written by the database clock, not the client."""
    return mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
