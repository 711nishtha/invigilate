"""Adversarial tests for attempt authorization.

Each test is named for the attack it attempts. These exercise
``app.services.attempt_access``, which is the gate Phase 4's student endpoints
call — putting the tests here means the guarantee is proven once rather than
re-proven per route.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.orm import Session

from app.core.errors import AttemptExpired, Conflict, NotFound
from app.core.security import generate_attempt_token
from app.db.models import AttemptStatus
from app.services.attempt_access import require_active_attempt, resolve_attempt
from tests.factories import full_attempt, make_attempt, make_quiz, make_user


class TestCrossStudentAccess:
    """A student must not reach another student's attempt by any route."""

    def test_cannot_read_another_students_attempt(self, db: Session) -> None:
        owner = make_user(db)
        quiz = make_quiz(db, owner)
        victim, _ = make_attempt(db, quiz, email="victim@example.edu")
        _, attacker_token = make_attempt(db, quiz, email="attacker@example.edu")

        # Attacker's own token, victim's attempt id.
        with pytest.raises(NotFound):
            resolve_attempt(db, attempt_id=victim.id, token=attacker_token)

    def test_cannot_submit_another_students_attempt(self, db: Session) -> None:
        owner = make_user(db)
        quiz = make_quiz(db, owner)
        victim, _ = make_attempt(db, quiz, email="victim@example.edu")
        _, attacker_token = make_attempt(db, quiz, email="attacker@example.edu")

        with pytest.raises(NotFound):
            resolve_attempt(db, attempt_id=victim.id, token=attacker_token, for_update=True)

    def test_cannot_answer_into_another_students_attempt(self, db: Session) -> None:
        """The gate refuses before any answer-writing code is reached."""
        owner = make_user(db)
        quiz = make_quiz(db, owner)
        victim, _ = make_attempt(db, quiz, email="victim@example.edu")
        _, attacker_token = make_attempt(db, quiz, email="attacker@example.edu")

        with pytest.raises(NotFound):
            context = resolve_attempt(db, attempt_id=victim.id, token=attacker_token)
            require_active_attempt(context)

    def test_a_token_resolves_only_its_own_attempt(self, db: Session) -> None:
        owner = make_user(db)
        quiz = make_quiz(db, owner)
        mine, my_token = make_attempt(db, quiz, email="mine@example.edu")
        theirs, _ = make_attempt(db, quiz, email="theirs@example.edu")

        assert resolve_attempt(db, attempt_id=mine.id, token=my_token).attempt.id == mine.id
        with pytest.raises(NotFound):
            resolve_attempt(db, attempt_id=theirs.id, token=my_token)


class TestIdentifierTampering:
    """Changing the id in the URL must not widen access."""

    def test_random_attempt_id_is_refused(self, db: Session) -> None:
        _, _, _ = full_attempt(db)
        _, token = make_attempt(db, make_quiz(db, make_user(db, "t2@example.edu")), email="s2@example.edu")
        with pytest.raises(NotFound):
            resolve_attempt(db, attempt_id=uuid.uuid4(), token=token)

    def test_malformed_attempt_id_is_refused_not_crashed(self, db: Session) -> None:
        attempt, token, _ = full_attempt(db)
        for malformed in ["", "not-a-uuid", "1 OR 1=1", "../../etc/passwd", str(attempt.id) + "x"]:
            with pytest.raises(NotFound):
                resolve_attempt(db, attempt_id=malformed, token=token)

    def test_attempt_id_as_string_still_works_for_the_rightful_owner(self, db: Session) -> None:
        attempt, token, _ = full_attempt(db)
        context = resolve_attempt(db, attempt_id=str(attempt.id), token=token)
        assert context.attempt.id == attempt.id


class TestTokenValidity:
    def test_empty_token_is_refused(self, db: Session) -> None:
        attempt, _, _ = full_attempt(db)
        with pytest.raises(NotFound):
            resolve_attempt(db, attempt_id=attempt.id, token="")

    def test_unknown_token_is_refused(self, db: Session) -> None:
        attempt, _, _ = full_attempt(db)
        with pytest.raises(NotFound):
            resolve_attempt(db, attempt_id=attempt.id, token=generate_attempt_token())

    def test_truncated_token_is_refused(self, db: Session) -> None:
        """A prefix of a valid token must not authenticate."""
        attempt, token, _ = full_attempt(db)
        with pytest.raises(NotFound):
            resolve_attempt(db, attempt_id=attempt.id, token=token[:-1])

    def test_token_hash_is_not_accepted_as_the_token(self, db: Session) -> None:
        """Someone who reads the database still cannot act as the student."""
        from app.core.security import hash_attempt_token

        attempt, token, _ = full_attempt(db)
        with pytest.raises(NotFound):
            resolve_attempt(db, attempt_id=attempt.id, token=hash_attempt_token(token))

    def test_every_failure_mode_raises_the_same_error(self, db: Session) -> None:
        """Wrong token, wrong id and no such attempt must be indistinguishable."""
        attempt, token, _ = full_attempt(db)
        messages = set()
        for attempt_id, bad_token in [
            (uuid.uuid4(), token),
            (attempt.id, generate_attempt_token()),
            (uuid.uuid4(), generate_attempt_token()),
        ]:
            with pytest.raises(NotFound) as caught:
                resolve_attempt(db, attempt_id=attempt_id, token=bad_token)
            messages.add((caught.value.code, caught.value.message))
        assert len(messages) == 1, "distinct errors would act as an existence oracle"


class TestExpiry:
    def test_expired_attempt_is_flagged_by_the_resolver(self, db: Session) -> None:
        attempt, token, _ = full_attempt(db, duration_seconds=60, started_offset_seconds=-3600)
        context = resolve_attempt(db, attempt_id=attempt.id, token=token)
        assert context.expired is True
        assert context.seconds_remaining == 0

    def test_expired_attempt_cannot_be_written_to(self, db: Session) -> None:
        attempt, token, _ = full_attempt(db, duration_seconds=60, started_offset_seconds=-3600)
        context = resolve_attempt(db, attempt_id=attempt.id, token=token)
        with pytest.raises(AttemptExpired):
            require_active_attempt(context)

    def test_live_attempt_is_writable(self, db: Session) -> None:
        attempt, token, _ = full_attempt(db)
        context = resolve_attempt(db, attempt_id=attempt.id, token=token)
        assert context.expired is False
        assert require_active_attempt(context).id == attempt.id

    def test_grace_window_does_not_extend_the_exam_indefinitely(self, db: Session) -> None:
        """Seconds past the buzzer are accepted; an hour is not."""
        from app.core.config import settings

        attempt, token, _ = full_attempt(
            db, duration_seconds=60, started_offset_seconds=-(60 + settings.attempt_grace_seconds - 2)
        )
        context = resolve_attempt(db, attempt_id=attempt.id, token=token)
        assert context.expired is False
        require_active_attempt(context)


class TestTokenReuseAfterFinalization:
    @pytest.mark.parametrize(
        "status",
        [AttemptStatus.SUBMITTED, AttemptStatus.AUTO_SUBMITTED, AttemptStatus.TIME_EXPIRED],
    )
    def test_token_cannot_write_after_a_terminal_status(
        self, db: Session, status: AttemptStatus
    ) -> None:
        attempt, token, _ = full_attempt(db)
        attempt.status = status
        db.commit()

        context = resolve_attempt(db, attempt_id=attempt.id, token=token)
        with pytest.raises(Conflict) as caught:
            require_active_attempt(context)
        assert caught.value.code == "ATTEMPT_ALREADY_FINALIZED"

    def test_token_can_still_read_its_own_submitted_attempt(self, db: Session) -> None:
        """A student is entitled to see their own result page after submitting."""
        attempt, token, _ = full_attempt(db)
        attempt.status = AttemptStatus.SUBMITTED
        db.commit()

        context = resolve_attempt(db, attempt_id=attempt.id, token=token)
        assert context.is_terminal is True
        assert context.attempt.id == attempt.id

    def test_a_submitted_attempt_cannot_be_reopened(self, db: Session) -> None:
        attempt, token, _ = full_attempt(db)
        attempt.status = AttemptStatus.SUBMITTED
        db.commit()

        context = resolve_attempt(db, attempt_id=attempt.id, token=token)
        with pytest.raises(Conflict):
            require_active_attempt(context)
        db.refresh(attempt)
        assert attempt.status is AttemptStatus.SUBMITTED


class TestEnumeration:
    def test_tokens_do_not_reveal_how_many_attempts_exist(self, db: Session) -> None:
        """Lookup is by token, so an id the caller guesses is never queried."""
        owner = make_user(db)
        quiz = make_quiz(db, owner)
        ids = []
        for index in range(5):
            created, _ = make_attempt(db, quiz, email=f"s{index}@example.edu")
            ids.append(created.id)
        _, outsider_token = make_attempt(
            db, make_quiz(db, make_user(db, "other@example.edu")), email="outsider@example.edu"
        )

        for attempt_id in ids:
            with pytest.raises(NotFound):
                resolve_attempt(db, attempt_id=attempt_id, token=outsider_token)


class TestTokenSecrecy:
    """Point 2: the plaintext token must not escape into logs or responses."""

    def test_token_fields_are_redacted_in_structured_logs(self) -> None:
        import json
        import logging

        from app.core.logging import JsonFormatter

        token = generate_attempt_token()
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "ATTEMPT_STARTED", (), None)
        record.attempt_token = token  # type: ignore[attr-defined]
        record.attempt_token_hash = "abc123"  # type: ignore[attr-defined]
        record.attempt_id = "visible"  # type: ignore[attr-defined]

        emitted = JsonFormatter().format(record)
        assert token not in emitted
        assert json.loads(emitted)["attempt_token"] == "[REDACTED]"
        assert json.loads(emitted)["attempt_id"] == "visible"

    def test_no_source_file_interpolates_a_token_into_a_log_message(self) -> None:
        """Redaction works on fields, not on message text, so interpolation is banned.

        This guards the rule rather than the mechanism: a future
        log.info(f"token={token}") would defeat redaction silently.
        """
        import pathlib
        import re

        risky = re.compile(r"log(?:ger)?\.\w+\([^)]*\btoken\b[^)]*\)", re.IGNORECASE)
        offenders = []
        for path in pathlib.Path("app").rglob("*.py"):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if risky.search(line) and "redact" not in line.lower():
                    offenders.append(f"{path}:{number}")
        assert not offenders, f"token interpolated into a log call: {offenders}"

    def test_token_comparison_is_constant_time(self) -> None:
        """Timing-safe comparison, verified at the source rather than by benchmark."""
        import inspect

        from app.core import security

        source = inspect.getsource(security.attempt_token_matches)
        assert "compare_digest" in source
        assert "==" not in source.split("return")[1]

    def test_only_the_hash_is_persisted(self, db: Session) -> None:
        attempt, token, _ = full_attempt(db)
        db.refresh(attempt)
        assert attempt.attempt_token_hash != token
        assert len(attempt.attempt_token_hash) == 64
        # The plaintext appears in no column of the row.
        for column in attempt.__table__.columns.keys():
            assert getattr(attempt, column) != token

    def test_tokens_have_at_least_256_bits_of_entropy(self) -> None:
        """secrets.token_urlsafe(32) is 32 random bytes, rendered as 43 characters."""
        import base64

        token = generate_attempt_token()
        decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        assert len(decoded) >= 32
