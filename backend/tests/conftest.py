"""Test fixtures.

Tests run against in-memory SQLite by default so the suite needs no container
and no network. Set TEST_DATABASE_URL to a PostgreSQL URL to exercise the
PostgreSQL-specific behaviour (native enums, row locking).

No test calls a paid API: the AI and email providers are stubs here.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

# Configure the environment before app modules read settings.
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_JWT_SECRET", "test-jwt-secret-not-a-real-secret")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
os.environ.setdefault("AI_PROVIDER", "stub")
os.environ.setdefault("EMAIL_PROVIDER", "stub")

import jwt  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.core.clock import utcnow  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.db.models import Base  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.main import create_app  # noqa: E402

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "sqlite+pysqlite:///:memory:")


@pytest.fixture(scope="session")
def engine():  # type: ignore[no-untyped-def]
    if TEST_DATABASE_URL.startswith("sqlite"):
        # StaticPool keeps one connection, so an in-memory database survives
        # across sessions within a test.
        eng = create_engine(
            TEST_DATABASE_URL,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    else:
        eng = create_engine(TEST_DATABASE_URL)
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture
def db(engine) -> Iterator[Session]:  # type: ignore[no-untyped-def]
    """A clean database per test."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(db: Session) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def make_jwt(
    subject: str = "11111111-1111-1111-1111-111111111111",
    email: str = "teacher@example.edu",
    *,
    expires_in: int = 3600,
    secret: str | None = None,
    audience: str | None = None,
) -> str:
    """Mint a Supabase-shaped JWT for tests."""
    now = int(utcnow().timestamp())
    return jwt.encode(
        {
            "sub": subject,
            "email": email,
            "aud": audience or settings.supabase_jwt_audience,
            "iat": now,
            "exp": now + expires_in,
        },
        secret or settings.supabase_jwt_secret,
        algorithm="HS256",
    )


@pytest.fixture
def teacher_token() -> str:
    return make_jwt()


@pytest.fixture
def auth_headers(teacher_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {teacher_token}"}
