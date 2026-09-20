"""Database engine and session management."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

_connect_args: dict[str, object] = {}
_engine_kwargs: dict[str, object] = {
    "echo": settings.db_echo,
    "pool_pre_ping": True,  # Supabase closes idle connections; check before use
    "future": True,
}

if settings.database_url.startswith("postgresql"):
    _engine_kwargs.update(
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_recycle=1800,
    )
    # pgBouncer in transaction mode cannot support server-side prepared
    # statements; psycopg negotiates this away when the threshold is disabled.
    _connect_args["prepare_threshold"] = None

engine = create_engine(settings.database_url, connect_args=_connect_args, **_engine_kwargs)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session.

    The session is rolled back on any exception so a failed request can never
    leave a half-applied exam state behind.
    """
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def check_database() -> bool:
    """Readiness probe. Returns False rather than raising."""
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 - probe reports health, never crashes
        return False
