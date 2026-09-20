"""PostgreSQL concurrency guarantees.

Everything here tests behaviour that SQLite cannot reproduce. Each test runs
two genuinely concurrent sessions against a real PostgreSQL and asserts the
outcome the exam engine depends on.

See docs/database.md, "PostgreSQL-specific behaviour", for the inventory of
what the application relies on and why.
"""

from __future__ import annotations

import threading
import time
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.security import generate_attempt_seed, hash_attempt_token
from app.db.models import Answer, Attempt, AttemptStatus, IntegrityStatus
from app.services.attempt_access import resolve_attempt
from app.services.attempt_finalization import finalize_attempt, finalize_if_expired
from tests.factories import answer, full_attempt, make_attempt, make_quiz, make_user
from tests.integration.conftest import requires_postgres

pytestmark = requires_postgres


def _run_concurrently(first, second, stagger: float = 0.05):  # type: ignore[no-untyped-def]
    """Run two callables on threads, starting the second after a short stagger.

    The stagger makes the interleaving deterministic enough to assert on: the
    first thread reaches the lock, the second arrives while it is held.
    """
    results: dict[str, object] = {}
    errors: dict[str, BaseException] = {}

    def wrap(name, fn):  # type: ignore[no-untyped-def]
        def runner() -> None:
            try:
                results[name] = fn()
            except BaseException as exc:  # noqa: BLE001 - recorded and re-raised by the test
                errors[name] = exc

        return runner

    thread_one = threading.Thread(target=wrap("first", first))
    thread_two = threading.Thread(target=wrap("second", second))
    thread_one.start()
    time.sleep(stagger)
    thread_two.start()
    thread_one.join(timeout=30)
    thread_two.join(timeout=30)
    return results, errors


class TestRowLocking:
    def test_select_for_update_blocks_a_second_reader(self, pg_session_factory) -> None:  # type: ignore[no-untyped-def]
        """The lock is what serializes concurrent submissions."""
        setup = pg_session_factory()
        attempt, token, _ = full_attempt(setup)
        attempt_id = attempt.id
        setup.close()

        order: list[str] = []
        holder_ready = threading.Event()

        def holder() -> None:
            session: Session = pg_session_factory()
            try:
                resolve_attempt(session, attempt_id=attempt_id, token=token, for_update=True)
                order.append("holder-acquired")
                holder_ready.set()
                time.sleep(1.0)  # keep the lock while the waiter tries
                order.append("holder-releasing")
                session.commit()
            finally:
                session.close()

        def waiter() -> None:
            holder_ready.wait(timeout=10)
            session: Session = pg_session_factory()
            try:
                resolve_attempt(session, attempt_id=attempt_id, token=token, for_update=True)
                order.append("waiter-acquired")
                session.commit()
            finally:
                session.close()

        _run_concurrently(holder, waiter, stagger=0.01)

        # The waiter must not acquire the lock before the holder released it.
        assert order.index("holder-releasing") < order.index("waiter-acquired"), order

    def test_locked_read_sees_the_committed_terminal_state(self, pg_session_factory) -> None:  # type: ignore[no-untyped-def]
        """The second submitter observes the first one's commit, not a stale row."""
        setup = pg_session_factory()
        attempt, token, _ = full_attempt(setup)
        attempt_id = attempt.id
        setup.close()

        first = pg_session_factory()
        context = resolve_attempt(first, attempt_id=attempt_id, token=token, for_update=True)
        finalize_attempt(first, context.attempt, reason="MANUAL")
        first.close()

        second = pg_session_factory()
        reread = resolve_attempt(second, attempt_id=attempt_id, token=token, for_update=True)
        assert reread.attempt.status is AttemptStatus.SUBMITTED
        result = finalize_attempt(second, reread.attempt, reason="MANUAL")
        assert result.already_final is True
        second.close()


class TestConcurrentSubmission:
    def test_two_simultaneous_submissions_produce_one_finalization(
        self, pg_session_factory
    ) -> None:  # type: ignore[no-untyped-def]
        setup = pg_session_factory()
        attempt, token, questions = full_attempt(setup, question_count=2)
        questions[0].options_order_json = None
        setup.commit()
        answer(setup, questions[0], {"displayed_index": 1})
        attempt_id = attempt.id
        setup.close()

        def submit() -> bool:
            session: Session = pg_session_factory()
            try:
                context = resolve_attempt(
                    session, attempt_id=attempt_id, token=token, for_update=True
                )
                return finalize_attempt(session, context.attempt, reason="MANUAL").already_final
            finally:
                session.close()

        results, errors = _run_concurrently(submit, submit, stagger=0.0)
        assert not errors, errors

        # Exactly one request did the finalizing.
        assert sorted([results["first"], results["second"]]) == [False, True]

        check = pg_session_factory()
        final = check.scalar(select(Attempt).where(Attempt.id == attempt_id))
        assert final is not None
        assert final.status is AttemptStatus.SUBMITTED
        assert final.score == Decimal("1.00")
        assert final.max_score == Decimal("2.00")
        check.close()

    def test_submission_racing_auto_submission(self, pg_session_factory) -> None:  # type: ignore[no-untyped-def]
        """Whichever lands first wins; the attempt never ends up in both states."""
        setup = pg_session_factory()
        attempt, token, _ = full_attempt(setup)
        attempt_id = attempt.id
        setup.close()

        def finalize(reason: str):  # type: ignore[no-untyped-def]
            def run() -> bool:
                session: Session = pg_session_factory()
                try:
                    context = resolve_attempt(
                        session, attempt_id=attempt_id, token=token, for_update=True
                    )
                    return finalize_attempt(session, context.attempt, reason=reason).already_final
                finally:
                    session.close()

            return run

        results, errors = _run_concurrently(
            finalize("AUTO_INTEGRITY"), finalize("MANUAL"), stagger=0.0
        )
        assert not errors, errors
        assert sorted([results["first"], results["second"]]) == [False, True]

        check = pg_session_factory()
        final = check.scalar(select(Attempt).where(Attempt.id == attempt_id))
        assert final is not None
        assert final.status in (AttemptStatus.AUTO_SUBMITTED, AttemptStatus.SUBMITTED)
        # If integrity won, the flag must have survived the losing submit.
        if final.status is AttemptStatus.AUTO_SUBMITTED:
            assert final.integrity_status is IntegrityStatus.FLAGGED
        check.close()

    def test_submission_racing_time_expiration(self, pg_session_factory) -> None:  # type: ignore[no-untyped-def]
        """An expired attempt finalizes once, whichever path reaches it first."""
        setup = pg_session_factory()
        attempt, token, _ = full_attempt(
            setup, duration_seconds=60, started_offset_seconds=-3600
        )
        attempt_id = attempt.id
        setup.close()

        def expire() -> bool:
            session: Session = pg_session_factory()
            try:
                context = resolve_attempt(
                    session, attempt_id=attempt_id, token=token, for_update=True
                )
                outcome = finalize_if_expired(session, context.attempt)
                return outcome is None or outcome.already_final
            finally:
                session.close()

        def submit() -> bool:
            session: Session = pg_session_factory()
            try:
                context = resolve_attempt(
                    session, attempt_id=attempt_id, token=token, for_update=True
                )
                return finalize_attempt(session, context.attempt, reason="MANUAL").already_final
            finally:
                session.close()

        results, errors = _run_concurrently(expire, submit, stagger=0.0)
        assert not errors, errors
        assert sorted([results["first"], results["second"]]) == [False, True]

        check = pg_session_factory()
        final = check.scalar(select(Attempt).where(Attempt.id == attempt_id))
        assert final is not None
        assert final.is_terminal
        assert final.submitted_at is not None
        check.close()


class TestUniqueConstraintsUnderRace:
    def test_two_simultaneous_starts_create_one_attempt(self, pg_session_factory) -> None:  # type: ignore[no-untyped-def]
        """UNIQUE (quiz_id, student_email) is the one-attempt rule under concurrency."""
        setup = pg_session_factory()
        owner = make_user(setup)
        quiz = make_quiz(setup, owner)
        quiz_id = quiz.id
        setup.close()

        def start() -> str:
            session: Session = pg_session_factory()
            try:
                quiz_row = session.scalar(select(type(quiz)).where(type(quiz).id == quiz_id))
                make_attempt(session, quiz_row, email="student@example.edu")
                return "created"
            except IntegrityError:
                session.rollback()
                return "rejected"
            finally:
                session.close()

        results, errors = _run_concurrently(start, start, stagger=0.0)
        assert not errors, errors
        assert sorted([results["first"], results["second"]]) == ["created", "rejected"]

        check = pg_session_factory()
        count = len(
            check.scalars(select(Attempt).where(Attempt.quiz_id == quiz_id)).all()
        )
        assert count == 1
        check.close()

    def test_concurrent_answer_writes_keep_one_row(self, pg_session_factory) -> None:  # type: ignore[no-untyped-def]
        """UNIQUE (attempt_question_id) makes an answer an upsert, not an append."""
        setup = pg_session_factory()
        _, _, questions = full_attempt(setup, question_count=1)
        question_id = questions[0].id
        setup.close()

        def write(index: int):  # type: ignore[no-untyped-def]
            def run() -> str:
                session: Session = pg_session_factory()
                try:
                    session.add(
                        Answer(
                            attempt_question_id=question_id,
                            selected_answer={"displayed_index": index},
                            answered_at=utcnow(),
                        )
                    )
                    session.commit()
                    return "written"
                except IntegrityError:
                    session.rollback()
                    return "rejected"
                finally:
                    session.close()

            return run

        results, errors = _run_concurrently(write(1), write(2), stagger=0.0)
        assert not errors, errors
        assert sorted([results["first"], results["second"]]) == ["rejected", "written"]

        check = pg_session_factory()
        rows = check.scalars(
            select(Answer).where(Answer.attempt_question_id == question_id)
        ).all()
        assert len(rows) == 1
        check.close()


class TestNativeEnums:
    def test_postgres_rejects_a_value_outside_the_enum(self, pg_db: Session) -> None:
        """The database is the last line of defence on attempt status."""
        attempt, _, _ = full_attempt(pg_db)
        with pytest.raises((DBAPIError, IntegrityError)):
            pg_db.execute(
                text("UPDATE attempts SET status = 'DEFINITELY_PASSED' WHERE id = :id"),
                {"id": str(attempt.id)},
            )
            pg_db.commit()
        pg_db.rollback()

    def test_enum_columns_store_values_not_python_member_names(self, pg_db: Session) -> None:
        attempt, _, _ = full_attempt(pg_db)
        stored = pg_db.execute(
            text("SELECT status::text FROM attempts WHERE id = :id"), {"id": str(attempt.id)}
        ).scalar_one()
        assert stored == "IN_PROGRESS", "must not be 'AttemptStatus.IN_PROGRESS'"

    def test_every_enum_type_exists_in_the_database(self, pg_db: Session) -> None:
        expected = {
            "user_role",
            "quiz_status",
            "security_level",
            "question_type",
            "generation_status",
            "validation_status",
            "attempt_status",
            "integrity_status",
            "integrity_event_type",
            "integrity_action",
        }
        present = {
            row[0]
            for row in pg_db.execute(
                text("SELECT typname FROM pg_type WHERE typtype = 'e'")
            ).all()
        }
        assert expected <= present, f"missing enum types: {expected - present}"


class TestTransactionBoundaries:
    def test_a_rolled_back_start_leaves_no_attempt(self, pg_session_factory) -> None:  # type: ignore[no-untyped-def]
        """A failure partway through attempt creation must leave nothing behind."""
        session: Session = pg_session_factory()
        owner = make_user(session)
        quiz = make_quiz(session, owner)

        session.add(
            Attempt(
                quiz_id=quiz.id,
                student_name="Rolled Back",
                student_email="rollback@example.edu",
                attempt_token_hash=hash_attempt_token("throwaway"),
                started_at=utcnow(),
                expires_at=utcnow(),
                seed=generate_attempt_seed(),
            )
        )
        session.flush()
        session.rollback()
        session.close()

        check = pg_session_factory()
        found = check.scalar(
            select(Attempt).where(Attempt.student_email == "rollback@example.edu")
        )
        assert found is None
        check.close()

    def test_finalization_commit_is_visible_to_other_sessions(self, pg_session_factory) -> None:  # type: ignore[no-untyped-def]
        setup = pg_session_factory()
        attempt, token, _ = full_attempt(setup)
        attempt_id = attempt.id
        setup.close()

        writer = pg_session_factory()
        context = resolve_attempt(writer, attempt_id=attempt_id, token=token, for_update=True)
        finalize_attempt(writer, context.attempt, reason="MANUAL")
        writer.close()

        reader = pg_session_factory()
        seen = reader.scalar(select(Attempt).where(Attempt.id == attempt_id))
        assert seen is not None and seen.status is AttemptStatus.SUBMITTED
        reader.close()
