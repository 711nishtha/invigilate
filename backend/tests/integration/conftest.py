"""Fixtures for PostgreSQL integration tests.

These tests need a real PostgreSQL because the behaviour under test —
``SELECT ... FOR UPDATE`` blocking, unique violations under concurrent inserts,
native enum rejection — does not exist on SQLite. They are skipped unless
TEST_DATABASE_URL points at PostgreSQL:

    docker compose up -d db
    TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5433/secure_quiz \\
        pytest tests/integration -v

Skipping is deliberate rather than silent: the suite reports the reason, so a
green run without PostgreSQL is never mistaken for one with it.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Base

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
IS_POSTGRES = TEST_DATABASE_URL.startswith("postgresql")

requires_postgres = pytest.mark.skipif(
    not IS_POSTGRES,
    reason=(
        "PostgreSQL integration tests need TEST_DATABASE_URL=postgresql+psycopg://... "
        "(see tests/integration/conftest.py)"
    ),
)


@pytest.fixture(scope="session")
def pg_engine() -> Iterator[Engine]:
    if not IS_POSTGRES:
        pytest.skip("TEST_DATABASE_URL is not a PostgreSQL URL")
    engine = create_engine(TEST_DATABASE_URL, poolclass=None)
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def pg_session_factory(pg_engine: Engine):  # type: ignore[no-untyped-def]
    """Session factory handing out independent connections.

    Concurrency tests need genuinely separate sessions — sharing one would
    serialize the very race being tested.
    """
    # Each test starts from a clean schema so that the unique-constraint tests
    # do not collide with rows left by an earlier test.
    Base.metadata.drop_all(pg_engine)
    Base.metadata.create_all(pg_engine)
    return sessionmaker(bind=pg_engine, autoflush=False, expire_on_commit=False)


@pytest.fixture
def pg_db(pg_session_factory) -> Iterator[Session]:  # type: ignore[no-untyped-def]
    session = pg_session_factory()
    try:
        yield session
    finally:
        session.close()
