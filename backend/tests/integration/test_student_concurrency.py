"""Student engine races, against real PostgreSQL.

SQLite cannot reproduce any of this: it serializes writes at the database
level, so the state-machine tests pass there without proving the locking. These
tests run two genuinely concurrent connections and assert the database cannot
reach an impossible state.

Skipped with a stated reason when TEST_DATABASE_URL is not a PostgreSQL URL.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.db.models import (
    Answer,
    Attempt,
    AttemptQuestion,
    AttemptStatus,
    IntegrityEventType,
    IntegrityStatus,
    Quiz,
    QuizStatus,
    VariantStatus,
)
from app.services.attempt_access import require_active_attempt, resolve_attempt
from app.services.attempt_finalization import finalize_attempt
from app.services.attempt_service import create_attempt, save_answer
from app.services.integrity_policy import record_event
from tests.factories import make_family, make_quiz, make_user, make_variant
from tests.integration.conftest import requires_postgres
from tests.integration.test_postgres_concurrency import _run_concurrently

pytestmark = requires_postgres


def _published_quiz(pg_session_factory, *, max_violations: int = 1) -> uuid.UUID:  # type: ignore[no-untyped-def]
    """A published quiz with one approved variant per family."""
    session: Session = pg_session_factory()
    teacher = make_user(session, email=f"t-{uuid.uuid4().hex[:8]}@example.edu")
    quiz = make_quiz(
        session,
        teacher,
        question_count=2,
        status=QuizStatus.PUBLISHED,
        public_code=uuid.uuid4().hex[:6].upper(),
        max_integrity_violations=max_violations,
    )
    for position in range(2):
        family = make_family(session, quiz, position=position)
        make_variant(session, family, validation_status=VariantStatus.APPROVED)
    session.commit()
    quiz_id = quiz.id
    session.close()
    return quiz_id


def _started_attempt(pg_session_factory, **kwargs) -> tuple[uuid.UUID, str, uuid.UUID]:  # type: ignore[no-untyped-def]
    quiz_id = _published_quiz(pg_session_factory, **kwargs)
    session: Session = pg_session_factory()
    quiz = session.get(Quiz, quiz_id)
    started = create_attempt(session, quiz, student_name="Asha", student_email="asha@uni.edu")
    attempt_id, token = started.attempt.id, started.token
    session.close()
    return attempt_id, token, quiz_id


class TestConcurrentAttemptCreation:
    def test_two_simultaneous_starts_create_one_attempt(self, pg_session_factory) -> None:  # type: ignore[no-untyped-def]
        """A double-clicked start button, or two tabs, must not yield two papers."""
        quiz_id = _published_quiz(pg_session_factory)

        def start() -> str:
            session: Session = pg_session_factory()
            try:
                quiz = session.get(Quiz, quiz_id)
                create_attempt(session, quiz, student_name="Asha", student_email="asha@uni.edu")
                return "created"
            except (AppError, IntegrityError):
                session.rollback()
                return "rejected"
            finally:
                session.close()

        results, errors = _run_concurrently(start, start, stagger=0.0)
        assert not errors, errors
        assert sorted([results["first"], results["second"]]) == ["created", "rejected"]

        check: Session = pg_session_factory()
        attempts = check.scalars(select(Attempt).where(Attempt.quiz_id == quiz_id)).all()
        assert len(attempts) == 1
        # The surviving attempt must still have its complete paper: a rolled
        # back loser must not have taken the winner's questions with it.
        questions = check.scalars(
            select(AttemptQuestion).where(AttemptQuestion.attempt_id == attempts[0].id)
        ).all()
        assert len(questions) == 2
        check.close()


class TestConcurrentSubmission:
    def test_two_simultaneous_submits_grade_exactly_once(self, pg_session_factory) -> None:  # type: ignore[no-untyped-def]
        attempt_id, token, _ = _started_attempt(pg_session_factory)

        setup: Session = pg_session_factory()
        context = resolve_attempt(setup, attempt_id=attempt_id, token=token)
        for question in setup.scalars(
            select(AttemptQuestion).where(AttemptQuestion.attempt_id == attempt_id)
        ).all():
            save_answer(setup, context.attempt, question.id, {"displayed_index": 0})
        setup.close()

        def submit() -> bool:
            session: Session = pg_session_factory()
            try:
                ctx = resolve_attempt(session, attempt_id=attempt_id, token=token, for_update=True)
                return finalize_attempt(session, ctx.attempt, reason="MANUAL").already_final
            finally:
                session.close()

        results, errors = _run_concurrently(submit, submit, stagger=0.0)
        assert not errors, errors
        assert sorted([results["first"], results["second"]]) == [False, True]

        check: Session = pg_session_factory()
        attempt = check.scalar(select(Attempt).where(Attempt.id == attempt_id))
        assert attempt.status is AttemptStatus.SUBMITTED
        assert attempt.max_score == Decimal("2.00")
        # Every answer must carry a verdict. A None here is the stale-relationship
        # bug returning: a submitted answer silently graded as unanswered.
        verdicts = [row.is_correct for row in check.scalars(select(Answer)).all()]
        assert verdicts and all(v is not None for v in verdicts)
        check.close()

    def test_answer_write_racing_finalization(self, pg_session_factory) -> None:  # type: ignore[no-untyped-def]
        """Whoever wins, the attempt must not end up half-finalized."""
        attempt_id, token, _ = _started_attempt(pg_session_factory)

        setup: Session = pg_session_factory()
        question_id = setup.scalars(
            select(AttemptQuestion).where(AttemptQuestion.attempt_id == attempt_id)
        ).first().id
        setup.close()

        def answer() -> str:
            session: Session = pg_session_factory()
            try:
                ctx = resolve_attempt(session, attempt_id=attempt_id, token=token)
                attempt = require_active_attempt(ctx)
                save_answer(session, attempt, question_id, {"displayed_index": 0})
                return "answered"
            except AppError:
                return "refused"
            finally:
                session.close()

        def submit() -> str:
            session: Session = pg_session_factory()
            try:
                ctx = resolve_attempt(session, attempt_id=attempt_id, token=token, for_update=True)
                finalize_attempt(session, ctx.attempt, reason="MANUAL")
                return "submitted"
            except AppError:
                return "refused"
            finally:
                session.close()

        results, errors = _run_concurrently(answer, submit, stagger=0.0)
        assert not errors, errors

        check: Session = pg_session_factory()
        attempt = check.scalar(select(Attempt).where(Attempt.id == attempt_id))
        assert attempt.is_terminal
        assert attempt.score is not None and attempt.max_score is not None
        assert attempt.submitted_at is not None
        check.close()

    def test_concurrent_answer_upserts_leave_one_row(self, pg_session_factory) -> None:  # type: ignore[no-untyped-def]
        attempt_id, token, _ = _started_attempt(pg_session_factory)

        setup: Session = pg_session_factory()
        question_id = setup.scalars(
            select(AttemptQuestion).where(AttemptQuestion.attempt_id == attempt_id)
        ).first().id
        setup.close()

        def write(index: int):  # type: ignore[no-untyped-def]
            def run() -> str:
                session: Session = pg_session_factory()
                try:
                    ctx = resolve_attempt(session, attempt_id=attempt_id, token=token)
                    attempt = require_active_attempt(ctx)
                    save_answer(session, attempt, question_id, {"displayed_index": index})
                    return "written"
                except (AppError, IntegrityError):
                    session.rollback()
                    return "refused"
                finally:
                    session.close()

            return run

        _results, errors = _run_concurrently(write(0), write(1), stagger=0.0)
        assert not errors, errors

        check: Session = pg_session_factory()
        rows = check.scalars(select(Answer).where(Answer.attempt_question_id == question_id)).all()
        assert len(rows) == 1, "an answer is an upsert, never a duplicate row"
        assert rows[0].is_correct is None, "correctness must not exist before finalization"
        check.close()


class TestIntegrityRaces:
    def test_integrity_threshold_racing_a_manual_submit(self, pg_session_factory) -> None:  # type: ignore[no-untyped-def]
        """Exactly one finalization, and a raised flag is never laundered away."""
        attempt_id, token, quiz_id = _started_attempt(pg_session_factory, max_violations=1)

        def integrity() -> str:
            session: Session = pg_session_factory()
            try:
                ctx = resolve_attempt(session, attempt_id=attempt_id, token=token, for_update=True)
                if ctx.attempt.is_terminal:
                    return "already-final"
                quiz = session.get(Quiz, quiz_id)
                # Severity 3 against a budget of 1 crosses the threshold at once.
                outcome = record_event(
                    session,
                    ctx.attempt,
                    quiz,
                    event_type=IntegrityEventType.SCREEN_SHARE_STOPPED,
                )
                if outcome.threshold_exceeded:
                    finalize_attempt(session, ctx.attempt, reason="AUTO_INTEGRITY")
                    return "auto-submitted"
                session.commit()
                return "recorded"
            except AppError:
                return "refused"
            finally:
                session.close()

        def submit() -> str:
            session: Session = pg_session_factory()
            try:
                ctx = resolve_attempt(session, attempt_id=attempt_id, token=token, for_update=True)
                result = finalize_attempt(session, ctx.attempt, reason="MANUAL")
                return "already-final" if result.already_final else "submitted"
            except AppError:
                return "refused"
            finally:
                session.close()

        _results, errors = _run_concurrently(integrity, submit, stagger=0.0)
        assert not errors, errors

        check: Session = pg_session_factory()
        attempt = check.scalar(select(Attempt).where(Attempt.id == attempt_id))
        assert attempt.is_terminal
        assert attempt.status in (AttemptStatus.SUBMITTED, AttemptStatus.AUTO_SUBMITTED)
        if attempt.status is AttemptStatus.AUTO_SUBMITTED:
            assert attempt.integrity_status is IntegrityStatus.FLAGGED
            assert attempt.integrity_violation_count >= 3
        check.close()

    def test_two_simultaneous_integrity_events_both_record(self, pg_session_factory) -> None:  # type: ignore[no-untyped-def]
        """Serialized by the row lock, so neither signal is lost to a lost update."""
        attempt_id, token, quiz_id = _started_attempt(pg_session_factory, max_violations=10)

        def report() -> str:
            session: Session = pg_session_factory()
            try:
                ctx = resolve_attempt(session, attempt_id=attempt_id, token=token, for_update=True)
                quiz = session.get(Quiz, quiz_id)
                record_event(
                    session, ctx.attempt, quiz, event_type=IntegrityEventType.TAB_HIDDEN
                )
                session.commit()
                return "recorded"
            finally:
                session.close()

        _results, errors = _run_concurrently(report, report, stagger=0.0)
        assert not errors, errors

        check: Session = pg_session_factory()
        attempt = check.scalar(select(Attempt).where(Attempt.id == attempt_id))
        assert attempt.integrity_violation_count == 2, "a lost update would show 1"
        check.close()


class TestNoImpossibleStates:
    def test_a_finalized_attempt_always_has_a_complete_result(
        self, pg_session_factory
    ) -> None:  # type: ignore[no-untyped-def]
        """Whatever races, a terminal attempt is never half-written."""
        attempt_id, token, quiz_id = _started_attempt(pg_session_factory, max_violations=1)

        def submit() -> str:
            session: Session = pg_session_factory()
            try:
                ctx = resolve_attempt(session, attempt_id=attempt_id, token=token, for_update=True)
                finalize_attempt(session, ctx.attempt, reason="MANUAL")
                return "submitted"
            except AppError:
                return "refused"
            finally:
                session.close()

        def integrity() -> str:
            session: Session = pg_session_factory()
            try:
                ctx = resolve_attempt(session, attempt_id=attempt_id, token=token, for_update=True)
                if ctx.attempt.is_terminal:
                    return "already-final"
                quiz = session.get(Quiz, quiz_id)
                outcome = record_event(
                    session, ctx.attempt, quiz,
                    event_type=IntegrityEventType.CAMERA_STOPPED,
                )
                if outcome.threshold_exceeded:
                    finalize_attempt(session, ctx.attempt, reason="AUTO_INTEGRITY")
                else:
                    session.commit()
                return "handled"
            except AppError:
                return "refused"
            finally:
                session.close()

        _results, errors = _run_concurrently(submit, integrity, stagger=0.0)
        assert not errors, errors

        check: Session = pg_session_factory()
        attempt = check.scalar(select(Attempt).where(Attempt.id == attempt_id))
        # The invariant: terminal implies fully finalized.
        assert attempt.is_terminal
        assert attempt.submitted_at is not None
        assert attempt.score is not None
        assert attempt.max_score is not None
        assert attempt.percentage is not None
        assert attempt.status in (AttemptStatus.SUBMITTED, AttemptStatus.AUTO_SUBMITTED)
        check.close()
