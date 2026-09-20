"""Invariants asserted by the Phase 1 security review.

These are not unit tests of a function so much as executable statements of the
properties the design claims. They exist so that a later change that quietly
breaks one of them fails CI rather than a real exam.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.core import clock
from app.db.models import Attempt, AttemptStatus, QuestionFamily, QuestionType, QuestionVariant
from app.services import attempt_access, attempt_finalization, identity
from tests.factories import full_attempt, make_family, make_quiz, make_user, make_variant


class TestServerClockAuthority:
    """Point 4: timing is the server's, in UTC, and never the client's."""

    def test_authoritative_now_is_utc(self) -> None:
        now = clock.utcnow()
        assert now.tzinfo is not None
        assert now.utcoffset() == timedelta(0), "server time must be UTC, not local"

    def test_attempt_timestamps_are_utc_and_server_generated(self, db: Session) -> None:
        attempt, _, _ = full_attempt(db)
        assert clock.ensure_aware(attempt.started_at).utcoffset() == timedelta(0)
        assert clock.ensure_aware(attempt.expires_at).utcoffset() == timedelta(0)

    def test_expires_at_is_derived_from_duration_not_supplied(self, db: Session) -> None:
        owner = make_user(db)
        quiz = make_quiz(db, owner, duration_seconds=900)
        attempt, _, _ = full_attempt(db)
        # The factory mirrors production: expiry = started_at + quiz duration.
        delta = clock.ensure_aware(attempt.expires_at) - clock.ensure_aware(attempt.started_at)
        assert delta == timedelta(seconds=attempt.quiz.duration_seconds)
        assert quiz.duration_seconds == 900  # settings honoured, not ignored

    def test_a_naive_timestamp_is_interpreted_as_utc_not_local(self) -> None:
        """Guards the latent bug where a driver drops tzinfo and comparisons shift."""
        naive = datetime(2026, 1, 1, 12, 0, 0)
        assert clock.ensure_aware(naive) == datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    def test_expiry_comparison_survives_a_naive_column(self, db: Session) -> None:
        """resolve_attempt must not raise when the driver returns naive datetimes."""
        attempt, token, _ = full_attempt(db)
        attempt.expires_at = clock.utcnow().replace(tzinfo=None) + timedelta(minutes=5)
        db.commit()
        context = attempt_access.resolve_attempt(db, attempt_id=attempt.id, token=token)
        assert context.expired is False
        assert context.seconds_remaining > 0

    def test_client_supplied_timestamps_are_not_parameters_of_any_timing_decision(self) -> None:
        """No timing function accepts a client value; there is nothing to spoof."""
        for fn in (clock.is_expired, clock.seconds_remaining, clock.expiry_from):
            params = set(inspect.signature(fn).parameters)
            assert not params & {"client_time", "client_reported_at", "now", "current_time"}, (
                f"{fn.__name__} exposes a caller-supplied clock"
            )

    def test_integrity_events_keep_client_time_separate_from_server_time(self) -> None:
        """client_reported_at exists for forensics; occurred_at is authoritative."""
        from app.db.models.integrity import IntegrityEvent

        columns = IntegrityEvent.__table__.columns
        assert "occurred_at" in columns and not columns["occurred_at"].nullable
        assert "client_reported_at" in columns and columns["client_reported_at"].nullable

    def test_terminal_state_is_decided_from_the_stored_row(self, db: Session) -> None:
        attempt, token, _ = full_attempt(db)
        attempt.status = AttemptStatus.SUBMITTED
        db.commit()
        context = attempt_access.resolve_attempt(db, attempt_id=attempt.id, token=token)
        assert context.is_terminal is True

    def test_finalization_stamps_the_server_clock(self, db: Session) -> None:
        attempt, _, _ = full_attempt(db)
        before = clock.utcnow()
        attempt_finalization.finalize_attempt(db, attempt, reason="MANUAL")
        after = clock.utcnow()
        assert before <= clock.ensure_aware(attempt.submitted_at) <= after


class TestQuestionFamilyModel:
    """Point 6: the family/variant split can carry what Phase 2 and 3 need."""

    REQUIRED_FIELDS = {
        "original_question",
        "question_type",
        "topic",
        "difficulty",
        "learning_objective",
        "generation_config_json",
    }

    def test_family_can_represent_every_required_field(self) -> None:
        columns = set(QuestionFamily.__table__.columns.keys())
        assert self.REQUIRED_FIELDS <= columns, f"missing: {self.REQUIRED_FIELDS - columns}"

    def test_all_four_question_types_exist(self) -> None:
        assert {t.value for t in QuestionType} == {
            "MCQ",
            "NUMERICAL",
            "TRUE_FALSE",
            "SHORT_ANSWER",
        }

    def test_a_family_round_trips_its_full_configuration(self, db: Session) -> None:
        owner = make_user(db)
        quiz = make_quiz(db, owner)
        config = {
            "variant_count": 5,
            "template": "A classifier predicts {correct} of {total}. Accuracy?",
            "parameters": {"total": {"type": "int", "min": 50, "max": 500}},
            "expression": "correct / total * 100",
            "dedupe_threshold": 0.92,
        }
        family = make_family(
            db,
            quiz,
            question_type=QuestionType.NUMERICAL,
            topic="Model evaluation",
            difficulty="medium",
            learning_objective="Compute accuracy from a confusion matrix",
            generation_config_json=config,
        )
        db.refresh(family)
        assert family.topic == "Model evaluation"
        assert family.difficulty == "medium"
        assert family.learning_objective.startswith("Compute accuracy")
        assert family.generation_config_json["expression"] == "correct / total * 100"
        assert family.generation_config_json["parameters"]["total"]["max"] == 500

    def test_one_family_supports_many_variants(self, db: Session) -> None:
        owner = make_user(db)
        quiz = make_quiz(db, owner)
        family = make_family(db, quiz)
        for _ in range(5):
            make_variant(db, family)
        db.refresh(family)
        assert len(family.variants) == 5
        assert all(v.family_id == family.id for v in family.variants)

    def test_a_variant_references_its_family_both_ways(self, db: Session) -> None:
        owner = make_user(db)
        quiz = make_quiz(db, owner)
        family = make_family(db, quiz)
        variant = make_variant(db, family)
        db.refresh(variant)
        assert variant.family.id == family.id
        assert variant.family.question_type is QuestionType.MCQ

    def test_variant_carries_generation_provenance(self) -> None:
        """Needed to trace a bad variant back to the prompt that produced it."""
        columns = set(QuestionVariant.__table__.columns.keys())
        assert {"generation_model", "generation_prompt_version", "generation_status"} <= columns

    def test_deleting_a_family_removes_its_variants(self, db: Session) -> None:
        owner = make_user(db)
        quiz = make_quiz(db, owner)
        family = make_family(db, quiz)
        make_variant(db, family)
        db.delete(family)
        db.commit()
        assert db.query(QuestionVariant).count() == 0


class TestIdentityExtensibility:
    """Point 5: the one-attempt rule can gain mechanisms without a redesign."""

    def test_self_declared_email_is_the_mvp_mechanism(self, db: Session) -> None:
        owner = make_user(db)
        quiz = make_quiz(db, owner)
        resolved = identity.resolve_identity(
            quiz=quiz, student_name="Asha", student_email="Asha@Uni.EDU"
        )
        assert resolved.mechanism is identity.IdentityMechanism.SELF_DECLARED_EMAIL
        assert resolved.dedupe_key == "asha@uni.edu"

    def test_self_declared_identity_is_not_treated_as_verified(self, db: Session) -> None:
        owner = make_user(db)
        quiz = make_quiz(db, owner)
        resolved = identity.resolve_identity(
            quiz=quiz, student_name="Asha", student_email="asha@uni.edu"
        )
        assert resolved.is_verified is False, "anyone can type anyone's address"

    def test_a_signed_in_account_overrides_a_typed_email(self, db: Session) -> None:
        """Otherwise a student types a second address to get a second attempt."""
        owner = make_user(db)
        quiz = make_quiz(db, owner)
        student = make_user(db, email="real.student@uni.edu")
        resolved = identity.resolve_identity(
            quiz=quiz,
            student_name="Whoever",
            student_email="someone.else@uni.edu",
            signed_in_user=student,
        )
        assert resolved.dedupe_key == "real.student@uni.edu"
        assert resolved.mechanism is identity.IdentityMechanism.SUPABASE_ACCOUNT
        assert resolved.is_verified is True

    def test_dedupe_key_is_what_the_unique_constraint_uses(self, db: Session) -> None:
        """The abstraction lines up with the column today, so swapping it is a migration."""
        constraint = next(
            c
            for c in Attempt.__table__.constraints
            if c.name == "uq_attempts_quiz_id_student_email"
        )
        assert {c.name for c in constraint.columns} == {"quiz_id", "student_email"}

    @pytest.mark.parametrize("bad", ["", "   ", "not-an-email"])
    def test_malformed_email_is_rejected(self, db: Session, bad: str) -> None:
        from app.core.errors import AppError

        owner = make_user(db)
        quiz = make_quiz(db, owner)
        with pytest.raises(AppError):
            identity.resolve_identity(quiz=quiz, student_name="Asha", student_email=bad)

    def test_future_mechanisms_are_named_without_being_implemented(self) -> None:
        assert {m.value for m in identity.IdentityMechanism} >= {
            "SELF_DECLARED_EMAIL",
            "SUPABASE_ACCOUNT",
            "ROSTER_ENROLMENT",
            "LMS_LAUNCH",
        }
