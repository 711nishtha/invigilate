"""Portable column types.

PostgreSQL is the production database and the only supported deployment target.
These decorators exist so the unit test suite can run against in-memory SQLite
without a container, which keeps the exam-engine tests fast. Anything that
depends on PostgreSQL semantics (row locking, native enums) is exercised against
PostgreSQL in the integration suite.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import CHAR, JSON, BigInteger, Dialect, Integer, TypeDecorator
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID


class GUIDType(TypeDecorator):
    """uuid4 primary/foreign keys.

    Native ``uuid`` on PostgreSQL; a 36-character string elsewhere, with
    explicit conversion in both directions so application code always sees a
    ``uuid.UUID``.
    """

    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> Any:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PGUUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        if not isinstance(value, uuid.UUID):
            value = uuid.UUID(str(value))
        if dialect.name == "postgresql":
            return value
        return str(value)

    def process_result_value(self, value: Any, dialect: Dialect) -> uuid.UUID | None:
        if value is None:
            return None
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


# Column aliases used by the models.
GUID = GUIDType()

# Structured payloads: option arrays, generation config, event metadata.
JSONColumn = JSONB().with_variant(JSON(), "sqlite")

# Attempt seeds are 63-bit; SQLite has no distinct BIGINT affinity.
BigInt = BigInteger().with_variant(Integer(), "sqlite")


def pg_enum(enum_cls: type, name: str) -> SAEnum:
    """Native PostgreSQL enum, VARCHAR + CHECK elsewhere.

    ``values_callable`` stores the string values rather than the Python member
    names, so the database contains 'TEACHER', not 'UserRole.TEACHER'.
    """
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=True,
        create_constraint=True,
        validate_strings=True,
        values_callable=lambda enum: [member.value for member in enum],
    )
