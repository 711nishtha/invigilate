"""Supabase PostgreSQL compatibility.

Local PostgreSQL equivalence is not sufficient evidence: Supabase's connection
poolers change transaction and prepared-statement semantics, and the exam
engine depends on both. These tests run against a real Supabase project.

    SUPABASE_TEST_DATABASE_URL=postgresql+psycopg://postgres.REF:PW@aws-0-REGION.pooler.supabase.com:6543/postgres \\
        pytest tests/integration/test_supabase_compatibility.py -v

Use a **scratch Supabase project**, never one holding real exam data: the
schema fixture creates and drops tables.

Skipped with a stated reason when the URL is absent, so a green run without
Supabase is never mistaken for a verified one.
"""

from __future__ import annotations

import os
import threading
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.clock import utcnow
from app.db.models import (
    Attempt,
    AttemptStatus,
    Base,
    QuizStatus,
    VariantStatus,
)
from app.services.attempt_finalization import finalize_attempt

SUPABASE_URL = os.environ.get("SUPABASE_TEST_DATABASE_URL", "")
IS_SUPABASE = SUPABASE_URL.startswith("postgresql")

requires_supabase = pytest.mark.skipif(
    not IS_SUPABASE,
    reason=(
        "Supabase compatibility needs SUPABASE_TEST_DATABASE_URL pointing at a "
        "scratch Supabase project (see this module's docstring)"
    ),
)

pytestmark = requires_supabase

# Port 6543 is the transaction pooler; 5432 is the direct/session connection.
IS_TRANSACTION_POOLER = ":6543" in SUPABASE_URL


@pytest.fixture(scope="module")
def supabase_engine():  # type: ignore[no-untyped-def]
    """Engine configured exactly as production configures it.

    ``prepare_threshold=None`` is the setting that matters: pgBouncer in
    transaction mode cannot hold server-side prepared statements, and psycopg
    otherwise creates them silently and fails on reuse.
    """
    engine = create_engine(
        SUPABASE_URL,
        connect_args={"prepare_threshold": None},
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
    )
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def supabase_sessions(supabase_engine):  # type: ignore[no-untyped-def]
    Base.metadata.drop_all(supabase_engine)
    Base.metadata.create_all(supabase_engine)
    return sessionmaker(bind=supabase_engine, autoflush=False, expire_on_commit=False)


def _seed(session_factory):  # type: ignore[no-untyped-def]
    from tests.factories import make_family, make_quiz, make_user, make_variant

    session: Session = session_factory()
    teacher = make_user(session, email=f"sb-{uuid.uuid4().hex[:8]}@example.edu")
    quiz = make_quiz(
        session,
        teacher,
        question_count=2,
        status=QuizStatus.PUBLISHED,
        public_code=uuid.uuid4().hex[:6].upper(),
    )
    for position in range(2):
        family = make_family(session, quiz, position=position)
        make_variant(session, family, validation_status=VariantStatus.APPROVED)
    session.commit()
    quiz_id = quiz.id
    session.close()
    return quiz_id


class TestConnection:
    def test_the_connection_works(self, supabase_engine) -> None:  # type: ignore[no-untyped-def]
        with supabase_engine.connect() as connection:
            assert connection.execute(text("SELECT 1")).scalar_one() == 1

    def test_the_server_is_postgresql(self, supabase_engine) -> None:  # type: ignore[no-untyped-def]
        with supabase_engine.connect() as connection:
            version = connection.execute(text("SELECT version()")).scalar_one()
        assert "PostgreSQL" in version
        print(f"\n  Supabase server: {version[:80]}")

    def test_which_connection_mode_is_in_use(self, supabase_engine) -> None:  # type: ignore[no-untyped-def]
        """Recorded in the output so the report can state it precisely."""
        mode = "transaction pooler (6543)" if IS_TRANSACTION_POOLER else "direct/session (5432)"
        print(f"\n  connection mode: {mode}")
        assert mode


class TestSchema:
    def test_every_table_was_created(self, supabase_engine) -> None:  # type: ignore[no-untyped-def]
        with supabase_engine.connect() as connection:
            present = {
                row[0]
                for row in connection.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname='public'")
                ).all()
            }
        assert set(Base.metadata.tables) <= present

    def test_native_enum_types_exist(self, supabase_engine) -> None:  # type: ignore[no-untyped-def]
        with supabase_engine.connect() as connection:
            present = {
                row[0]
                for row in connection.execute(
                    text("SELECT typname FROM pg_type WHERE typtype='e'")
                ).all()
            }
        expected = {
            "user_role",
            "quiz_status",
            "variant_status",
            "attempt_status",
            "integrity_status",
            "delivery_status",
        }
        assert expected <= present, f"missing: {expected - present}"

    def test_an_invalid_enum_value_is_refused(self, supabase_sessions) -> None:  # type: ignore[no-untyped-def]
        quiz_id = _seed(supabase_sessions)
        session: Session = supabase_sessions()
        from tests.factories import make_attempt
        from app.db.models import Quiz

        attempt, _ = make_attempt(session, session.get(Quiz, quiz_id))
        with pytest.raises((DBAPIError, IntegrityError)):
            session.execute(
                text("UPDATE attempts SET status='NOT_A_STATUS' WHERE id=:id"),
                {"id": str(attempt.id)},
            )
            session.commit()
        session.rollback()
        session.close()

    def test_unique_constraints_are_enforced(self, supabase_sessions) -> None:  # type: ignore[no-untyped-def]
        from app.db.models import Quiz
        from tests.factories import make_attempt

        quiz_id = _seed(supabase_sessions)
        session: Session = supabase_sessions()
        quiz = session.get(Quiz, quiz_id)
        make_attempt(session, quiz, email="dupe@example.edu")
        with pytest.raises(IntegrityError):
            make_attempt(session, quiz, email="dupe@example.edu")
        session.rollback()
        session.close()

    def test_uuid_and_jsonb_round_trip(self, supabase_sessions) -> None:  # type: ignore[no-untyped-def]
        from app.db.models import Quiz

        quiz_id = _seed(supabase_sessions)
        session: Session = supabase_sessions()
        quiz = session.get(Quiz, quiz_id)
        assert isinstance(quiz.id, uuid.UUID)

        quiz.coverage_config_json = {"required_topics": ["Trees", "Grafos ñ 王"]}
        session.commit()
        session.expunge_all()

        reloaded = session.get(Quiz, quiz_id)
        assert reloaded.coverage_config_json["required_topics"] == ["Trees", "Grafos ñ 王"]
        session.close()

    def test_timestamps_round_trip_as_utc_aware(self, supabase_sessions) -> None:  # type: ignore[no-untyped-def]
        from app.db.models import Quiz
        from tests.factories import make_attempt

        quiz_id = _seed(supabase_sessions)
        session: Session = supabase_sessions()
        attempt, _ = make_attempt(session, session.get(Quiz, quiz_id))
        session.expunge_all()

        reloaded = session.get(Attempt, attempt.id)
        assert reloaded.started_at.tzinfo is not None, "timestamptz must come back aware"
        assert reloaded.started_at.utcoffset() == timedelta(0)
        session.close()


class TestTransactionsAndLocking:
    """The behaviour a pooler is most likely to change."""

    def test_a_rollback_leaves_nothing_behind(self, supabase_sessions) -> None:  # type: ignore[no-untyped-def]
        from app.db.models import Quiz
        from tests.factories import make_attempt

        quiz_id = _seed(supabase_sessions)
        session: Session = supabase_sessions()
        quiz = session.get(Quiz, quiz_id)
        make_attempt(session, quiz, email="rollback@example.edu")
        session.rollback()
        session.close()

        check: Session = supabase_sessions()
        found = check.scalar(
            select(Attempt).where(Attempt.student_email == "rollback@example.edu")
        )
        # make_attempt commits, so this asserts the commit stuck rather than the
        # rollback; the meaningful check is the explicit one below.
        check.close()

        session2: Session = supabase_sessions()
        quiz2 = session2.get(Quiz, quiz_id)
        attempt = Attempt(
            quiz_id=quiz2.id,
            student_name="Never Committed",
            student_email="never@example.edu",
            attempt_token_hash="x" * 64,
            started_at=utcnow(),
            expires_at=utcnow() + timedelta(minutes=10),
            seed=1234,
        )
        session2.add(attempt)
        session2.flush()
        session2.rollback()
        session2.close()

        check2: Session = supabase_sessions()
        assert (
            check2.scalar(select(Attempt).where(Attempt.student_email == "never@example.edu"))
            is None
        )
        check2.close()

    def test_select_for_update_serializes_two_connections(self, supabase_sessions) -> None:  # type: ignore[no-untyped-def]
        """The lock the whole submission model rests on.

        A transaction pooler that broke this would silently allow double
        finalization, so it is verified against Supabase directly rather than
        assumed from local PostgreSQL.
        """
        import time

        from app.db.models import Quiz
        from app.services.attempt_access import resolve_attempt
        from tests.factories import make_attempt

        quiz_id = _seed(supabase_sessions)
        setup: Session = supabase_sessions()
        attempt, token = make_attempt(setup, setup.get(Quiz, quiz_id))
        attempt_id = attempt.id
        setup.close()

        order: list[str] = []
        holder_ready = threading.Event()

        def holder() -> None:
            session: Session = supabase_sessions()
            try:
                resolve_attempt(session, attempt_id=attempt_id, token=token, for_update=True)
                order.append("holder-acquired")
                holder_ready.set()
                time.sleep(1.5)
                order.append("holder-releasing")
                session.commit()
            finally:
                session.close()

        def waiter() -> None:
            holder_ready.wait(timeout=10)
            time.sleep(0.1)
            session: Session = supabase_sessions()
            try:
                resolve_attempt(session, attempt_id=attempt_id, token=token, for_update=True)
                order.append("waiter-acquired")
                session.commit()
            finally:
                session.close()

        threads = [threading.Thread(target=holder), threading.Thread(target=waiter)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert "waiter-acquired" in order, "the waiter never acquired the lock"
        assert order.index("holder-releasing") < order.index("waiter-acquired"), order

    def test_concurrent_finalization_happens_once(self, supabase_sessions) -> None:  # type: ignore[no-untyped-def]
        from app.db.models import Quiz
        from app.services.attempt_access import resolve_attempt
        from tests.factories import make_attempt, make_paper

        quiz_id = _seed(supabase_sessions)
        setup: Session = supabase_sessions()
        quiz = setup.get(Quiz, quiz_id)
        attempt, token = make_attempt(setup, quiz)
        variants = [family.variants[0] for family in quiz.question_families]
        make_paper(setup, attempt, variants)
        attempt_id = attempt.id
        setup.close()

        results: dict[str, bool] = {}

        def submit(name: str) -> None:
            session: Session = supabase_sessions()
            try:
                context = resolve_attempt(
                    session, attempt_id=attempt_id, token=token, for_update=True
                )
                results[name] = finalize_attempt(
                    session, context.attempt, reason="MANUAL"
                ).already_final
            finally:
                session.close()

        threads = [
            threading.Thread(target=submit, args=("first",)),
            threading.Thread(target=submit, args=("second",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert sorted(results.values()) == [False, True], results

        check: Session = supabase_sessions()
        final = check.get(Attempt, attempt_id)
        assert final.status is AttemptStatus.SUBMITTED
        assert final.score is not None
        check.close()


class TestPreparedStatements:
    def test_repeated_parameterized_queries_work(self, supabase_sessions) -> None:  # type: ignore[no-untyped-def]
        """The pgBouncer failure mode this configuration exists to avoid.

        With prepare_threshold left at its default, psycopg creates a
        server-side prepared statement after a few executions; through a
        transaction pooler the next execution can land on a different backend
        and fail with "prepared statement does not exist".
        """
        quiz_id = _seed(supabase_sessions)
        session: Session = supabase_sessions()
        from app.db.models import Quiz

        for _ in range(30):
            found = session.scalar(select(Quiz).where(Quiz.id == quiz_id))
            assert found is not None
        session.close()

    def test_many_sequential_sessions_work(self, supabase_sessions) -> None:  # type: ignore[no-untyped-def]
        """Mimics one request per session, which is how the app runs."""
        quiz_id = _seed(supabase_sessions)
        from app.db.models import Quiz

        for _ in range(25):
            session: Session = supabase_sessions()
            assert session.scalar(select(Quiz).where(Quiz.id == quiz_id)) is not None
            session.close()


class TestAlembicAgainstSupabase:
    def test_migrations_apply_from_zero(self, supabase_engine) -> None:  # type: ignore[no-untyped-def]
        """Runs the real migration history, not metadata.create_all.

        Skipped on the transaction pooler: DDL belongs on the direct
        connection, and Alembic's transactional DDL is not safe through
        pgBouncer in transaction mode.
        """
        if IS_TRANSACTION_POOLER:
            pytest.skip(
                "Migrations must run on the direct connection (port 5432), not the "
                "transaction pooler; re-run with the direct URL to verify this."
            )

        from alembic import command
        from alembic.config import Config

        Base.metadata.drop_all(supabase_engine)
        with supabase_engine.connect() as connection:
            connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
            for enum_name in (
                "user_role", "quiz_status", "security_level", "question_type",
                "generation_status", "variant_status", "validation_status",
                "attempt_status", "integrity_status", "integrity_event_type",
                "integrity_action", "delivery_kind", "delivery_status",
            ):
                connection.execute(text(f"DROP TYPE IF EXISTS {enum_name} CASCADE"))
            connection.commit()

        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", SUPABASE_URL)
        command.upgrade(config, "head")

        with supabase_engine.connect() as connection:
            version = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        assert version == "0003_result_deliveries"
