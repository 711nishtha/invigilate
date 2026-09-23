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
os.environ.setdefault("STORAGE_PROVIDER", "stub")

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


# --- Phase 2 fixtures ------------------------------------------------------

TEACHER_A_SUB = "aaaaaaaa-0000-0000-0000-000000000001"
TEACHER_B_SUB = "bbbbbbbb-0000-0000-0000-000000000002"


def headers_for(subject: str, email: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_jwt(subject=subject, email=email)}"}


def register_teacher(client: TestClient, subject: str, email: str, name: str) -> dict[str, str]:
    """Create a teacher profile through the real endpoint and return its headers."""
    headers = headers_for(subject, email)
    response = client.post("/api/auth/profile", json={"name": name}, headers=headers)
    assert response.status_code == 200, response.text
    return headers


@pytest.fixture
def teacher_a(client: TestClient) -> dict[str, str]:
    return register_teacher(client, TEACHER_A_SUB, "teacher.a@example.edu", "Teacher A")


@pytest.fixture
def teacher_b(client: TestClient) -> dict[str, str]:
    """A second teacher, for cross-tenant authorization tests."""
    return register_teacher(client, TEACHER_B_SUB, "teacher.b@example.edu", "Teacher B")


@pytest.fixture(autouse=True)
def stub_ai_provider():
    """Every test runs against the stub provider unless it installs its own.

    Guarantees no test can reach a paid API even if configuration drifts.
    """
    from app.ai.factory import set_ai_provider
    from app.ai.stub import StubProvider

    set_ai_provider(StubProvider())
    yield
    set_ai_provider(None)


@pytest.fixture(autouse=True)
def disable_rate_limiting():
    """Turn the limiter off for ordinary tests.

    slowapi's limiter is process-global and keys on the client IP, which is the
    same 'testclient' for every test. Left on, the suite exhausts the budget
    and unrelated tests start failing with 429s. Tests that exercise rate
    limiting itself use the `rate_limited` fixture to switch it back on.
    """
    from app.core.rate_limit import limiter

    limiter.enabled = False
    yield
    limiter.enabled = True


@pytest.fixture
def rate_limited():
    """Enable rate limiting, with fresh storage, for one test."""
    from app.core.rate_limit import limiter

    limiter.enabled = True
    try:
        limiter._storage.reset()  # type: ignore[attr-defined]
    except (AttributeError, NotImplementedError):
        pass
    yield limiter
    limiter.enabled = False


@pytest.fixture(autouse=True)
def stub_external_providers():
    """Email and storage stubs for every test.

    Same reasoning as the AI stub: no test run may send a real email, write to
    a real bucket, or spend money, even if configuration drifts.
    """
    from app.email.factory import set_email_provider
    from app.email.stub import StubEmailProvider
    from app.storage.factory import set_storage_provider
    from app.storage.stub import StubStorageProvider

    email = StubEmailProvider()
    storage = StubStorageProvider()
    set_email_provider(email)
    set_storage_provider(storage)
    yield {"email": email, "storage": storage}
    set_email_provider(None)
    set_storage_provider(None)
