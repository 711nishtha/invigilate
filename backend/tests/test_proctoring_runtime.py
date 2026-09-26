"""Proctoring during the exam: heartbeats, degradation and stream loss.

The claims under test:

* the browser reports observations and never a conclusion;
* one bad frame is never a decision, and the debounce that guarantees it lives
  on the server;
* monitoring that stops becomes a visible state, never a silent pass;
* a proctoring failure can end an exam early but can never change a mark.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.db.models import (
    Attempt,
    AttemptStatus,
    IntegrityEvent,
    IntegrityEventType,
    ProctoringMode,
    ProctoringStatus,
    Quiz,
)
from app.services import integrity_policy, proctoring_policy, proctoring_service
from tests.factories import full_attempt

ALL_RUNNING = {
    "camera_active": True,
    "microphone_active": True,
    "screen_share_active": True,
    "fullscreen_active": True,
}


def proctored_attempt(
    db: Session,
    *,
    config: dict | None = None,
    max_integrity_violations: int | None = None,
    **kwargs,
) -> tuple[Attempt, str, Quiz]:  # type: ignore[no-untyped-def]
    """An in-progress attempt on a PROCTORED quiz, ready to heartbeat."""
    attempt, token, _questions = full_attempt(db, **kwargs)
    quiz = db.get(Quiz, attempt.quiz_id)
    assert quiz is not None
    if max_integrity_violations is not None:
        quiz.max_integrity_violations = max_integrity_violations
    quiz.proctoring_mode = ProctoringMode.PROCTORED
    quiz.proctoring_config_json = config if config is not None else {"require_camera": True}
    attempt.proctoring_mode = ProctoringMode.PROCTORED
    attempt.proctoring_status = ProctoringStatus.ACTIVE
    attempt.last_heartbeat_at = utcnow()
    db.commit()
    db.refresh(attempt)
    return attempt, token, quiz


def beat(client: TestClient, attempt: Attempt, token: str, **observations) -> dict:
    response = client.post(
        f"/api/student/attempts/{attempt.id}/proctoring/heartbeat",
        json=observations,
        headers={"X-Attempt-Token": token},
    )
    assert response.status_code == 200, response.text
    return response.json()


class TestTheHeartbeatCarriesNoVerdict:
    @pytest.mark.parametrize(
        "forbidden",
        [
            {"severity": 0},
            {"violation_count": 0},
            {"suspicion_score": 0.1},
            {"integrity_status": "CLEAN"},
            {"proctoring_status": "ACTIVE"},
            {"is_cheating": False},
            {"auto_submitted": False},
        ],
    )
    def test_a_conclusion_field_is_rejected(
        self, client: TestClient, db: Session, forbidden: dict
    ) -> None:
        attempt, token, _ = proctored_attempt(db)
        response = client.post(
            f"/api/student/attempts/{attempt.id}/proctoring/heartbeat",
            json={**ALL_RUNNING, **forbidden},
            headers={"X-Attempt-Token": token},
        )
        assert response.status_code == 422

    def test_the_request_model_cannot_express_a_conclusion(self) -> None:
        from app.schemas.proctoring import ProctoringHeartbeatRequest

        fields = set(ProctoringHeartbeatRequest.model_fields)
        assert not fields & {
            "severity",
            "violation_count",
            "suspicion_score",
            "integrity_status",
            "proctoring_status",
            "is_cheating",
            "verdict",
        }

    def test_the_server_sets_the_cadence_not_the_client(
        self, client: TestClient, db: Session
    ) -> None:
        attempt, token, quiz = proctored_attempt(db)
        body = beat(client, attempt, token, **ALL_RUNNING)
        expected = proctoring_policy.policy_for(quiz).heartbeat_interval_seconds
        assert body["next_heartbeat_seconds"] == expected


class TestAHealthyHeartbeat:
    def test_everything_running_records_no_signal(
        self, client: TestClient, db: Session
    ) -> None:
        attempt, token, _ = proctored_attempt(db)
        body = beat(client, attempt, token, **ALL_RUNNING)
        assert body["signals_recorded"] == []
        assert body["violation_total"] == 0
        assert body["proctoring_status"] == "ACTIVE"

    def test_the_server_records_when_it_heard_from_the_client(
        self, client: TestClient, db: Session
    ) -> None:
        attempt, token, _ = proctored_attempt(db)
        attempt.last_heartbeat_at = utcnow() - timedelta(seconds=45)
        db.commit()
        beat(client, attempt, token, **ALL_RUNNING)
        db.refresh(attempt)
        assert utcnow() - attempt.last_heartbeat_at.replace(
            tzinfo=attempt.last_heartbeat_at.tzinfo or utcnow().tzinfo
        ) < timedelta(seconds=5)

    def test_an_unrequired_stream_being_off_is_not_a_signal(
        self, client: TestClient, db: Session
    ) -> None:
        """The policy requires a camera only, so a silent mic is irrelevant."""
        attempt, token, _ = proctored_attempt(db, config={"require_camera": True})
        body = beat(client, attempt, token, camera_active=True, microphone_active=False)
        assert body["signals_recorded"] == []


class TestStreamLoss:
    def test_a_required_camera_going_dark_is_recorded(
        self, client: TestClient, db: Session
    ) -> None:
        attempt, token, _ = proctored_attempt(db)
        body = beat(client, attempt, token, camera_active=False)
        assert "CAMERA_STOPPED" in body["signals_recorded"]
        assert body["proctoring_status"] == "DEGRADED"
        assert body["violation_total"] > 0

    def test_the_student_is_warned_rather_than_accused(
        self, client: TestClient, db: Session
    ) -> None:
        attempt, token, _ = proctored_attempt(db)
        body = beat(client, attempt, token, camera_active=False)
        warning = " ".join(body["warnings"]).lower()
        assert warning
        for banned in ("cheat", "cheating", "violation of academic", "misconduct"):
            assert banned not in warning

    def test_the_default_action_does_not_end_the_exam(
        self, client: TestClient, db: Session
    ) -> None:
        """WARN is the default: a dead webcam driver must not end an exam.

        The budget is raised here so this test measures the stream-loss action
        alone. On a quiz left at the default budget of 2 the exam *does* end,
        because CAMERA_STOPPED is worth 3 — see
        ``test_the_default_budget_is_smaller_than_a_stopped_stream`` below.
        """
        attempt, token, _ = proctored_attempt(db, max_integrity_violations=10)
        body = beat(client, attempt, token, camera_active=False)
        assert body["auto_submitted"] is False
        assert body["attempt_status"] == "IN_PROGRESS"

    def test_auto_submit_is_available_when_a_teacher_chooses_it(
        self, client: TestClient, db: Session
    ) -> None:
        attempt, token, _ = proctored_attempt(
            db, config={"require_camera": True, "on_stream_loss": "AUTO_SUBMIT"}
        )
        body = beat(client, attempt, token, camera_active=False)
        assert body["auto_submitted"] is True
        db.refresh(attempt)
        assert attempt.status is AttemptStatus.AUTO_SUBMITTED

    def test_an_auto_submit_still_grades_the_paper(
        self, client: TestClient, db: Session
    ) -> None:
        """A proctoring decision ends an exam; it never corrupts the result."""
        attempt, token, _ = proctored_attempt(
            db, config={"require_camera": True, "on_stream_loss": "AUTO_SUBMIT"}
        )
        beat(client, attempt, token, camera_active=False)
        db.refresh(attempt)
        assert attempt.score is not None
        assert attempt.max_score is not None
        assert attempt.submitted_at is not None

    def test_the_default_budget_is_smaller_than_a_stopped_stream(
        self, client: TestClient, db: Session
    ) -> None:
        """Documented consequence, pinned so it cannot change unnoticed.

        CAMERA_STOPPED is worth 3 and the default max_integrity_violations is
        2, so a PROCTORED quiz left at the default ends the exam the first time
        a camera drops — regardless of on_stream_loss. That is a configuration
        choice a teacher should make deliberately, so it is stated in
        docs/proctoring.md rather than quietly softened here.
        """
        attempt, token, _ = proctored_attempt(db)
        assert db.get(Quiz, attempt.quiz_id).max_integrity_violations == 2
        body = beat(client, attempt, token, camera_active=False)
        assert body["auto_submitted"] is True

    def test_recovery_returns_the_attempt_to_active(
        self, client: TestClient, db: Session
    ) -> None:
        """A student who re-enables their camera is not branded for the rest."""
        attempt, token, _ = proctored_attempt(db, max_integrity_violations=10)
        beat(client, attempt, token, camera_active=False)
        body = beat(client, attempt, token, camera_active=True)
        assert body["proctoring_status"] == "ACTIVE"


class TestHonestImpairmentIsFree:
    def test_reporting_impairment_costs_no_severity(
        self, client: TestClient, db: Session
    ) -> None:
        """Punishing an honest client trains clients to lie."""
        attempt, token, _ = proctored_attempt(db)
        body = beat(
            client,
            attempt,
            token,
            **ALL_RUNNING,
            monitoring_impaired=True,
            impaired_reason="face detector unavailable",
        )
        assert "PROCTORING_UNAVAILABLE" in body["signals_recorded"]
        assert body["violation_total"] == 0

    def test_but_it_does_change_the_status(
        self, client: TestClient, db: Session
    ) -> None:
        """Free, but never invisible: it must not look like a clean exam."""
        attempt, token, _ = proctored_attempt(db)
        body = beat(client, attempt, token, **ALL_RUNNING, monitoring_impaired=True)
        assert body["proctoring_status"] == "DEGRADED"


class TestOneFrameIsNeverADecision:
    """The server-side repeat window, which is the client-bug defence."""

    def test_a_repeated_signal_inside_the_window_costs_nothing(
        self, db: Session
    ) -> None:
        attempt, _token, _q = full_attempt(db)
        quiz = db.get(Quiz, attempt.quiz_id)
        assert quiz is not None

        first = integrity_policy.record_event(
            db, attempt, quiz, event_type=IntegrityEventType.FACE_MISSING
        )
        db.commit()
        second = integrity_policy.record_event(
            db, attempt, quiz, event_type=IntegrityEventType.FACE_MISSING
        )
        db.commit()

        assert first.event.severity == 2
        assert second.event.severity == 0
        assert attempt.integrity_violation_count == 2

    def test_the_suppression_is_visible_in_the_timeline(self, db: Session) -> None:
        """Suppressed is not the same as unrecorded: review still sees it."""
        attempt, _token, _q = full_attempt(db)
        quiz = db.get(Quiz, attempt.quiz_id)
        assert quiz is not None
        integrity_policy.record_event(
            db, attempt, quiz, event_type=IntegrityEventType.FACE_MISSING
        )
        second = integrity_policy.record_event(
            db, attempt, quiz, event_type=IntegrityEventType.FACE_MISSING
        )
        db.commit()
        assert second.event.metadata_json.get("repeat_suppressed") is True

    def test_a_flood_cannot_auto_submit_an_honest_attempt(
        self, client: TestClient, db: Session
    ) -> None:
        """A client stuck in a loop must not walk a student out of their exam."""
        attempt, token, _ = proctored_attempt(db)
        for _ in range(40):
            client.post(
                f"/api/student/attempts/{attempt.id}/integrity-events",
                json={"event_type": "FACE_MISSING"},
                headers={"X-Attempt-Token": token},
            )
        db.refresh(attempt)
        assert attempt.status is AttemptStatus.IN_PROGRESS
        # One counted occurrence, not forty.
        assert attempt.integrity_violation_count == 2

    def test_phase_three_signals_are_unaffected_by_the_window(
        self, db: Session
    ) -> None:
        """No Phase 3 event type has a repeat window, so nothing changed."""
        attempt, _token, _q = full_attempt(db)
        quiz = db.get(Quiz, attempt.quiz_id)
        assert quiz is not None
        for _ in range(3):
            integrity_policy.record_event(
                db, attempt, quiz, event_type=IntegrityEventType.TAB_HIDDEN
            )
        db.commit()
        assert attempt.integrity_violation_count == 3


class TestSilenceIsNoticed:
    def test_a_quiet_browser_is_caught_on_an_ordinary_request(
        self, client: TestClient, db: Session
    ) -> None:
        """A client can stop calling the endpoint that reports on itself.

        So silence is detected on the paths it cannot avoid.
        """
        attempt, token, _ = proctored_attempt(db)
        attempt.last_heartbeat_at = utcnow() - timedelta(seconds=600)
        db.commit()

        response = client.get(
            f"/api/student/attempts/{attempt.id}",
            headers={"X-Attempt-Token": token},
        )
        assert response.status_code == 200
        assert response.json()["proctoring_status"] == "UNVERIFIABLE"

    def test_the_gap_is_recorded_with_its_length(
        self, client: TestClient, db: Session
    ) -> None:
        attempt, token, _ = proctored_attempt(db)
        attempt.last_heartbeat_at = utcnow() - timedelta(seconds=600)
        db.commit()
        client.get(
            f"/api/student/attempts/{attempt.id}", headers={"X-Attempt-Token": token}
        )
        event = (
            db.query(IntegrityEvent)
            .filter(
                IntegrityEvent.attempt_id == attempt.id,
                IntegrityEvent.event_type == IntegrityEventType.PROCTORING_HEARTBEAT_LOST,
            )
            .one()
        )
        assert event.metadata_json["silent_seconds"] >= 590

    def test_silence_does_not_end_the_exam(
        self, client: TestClient, db: Session
    ) -> None:
        """A closed laptop and a dead router look identical from the server."""
        attempt, token, _ = proctored_attempt(db)
        attempt.last_heartbeat_at = utcnow() - timedelta(seconds=600)
        db.commit()
        client.get(
            f"/api/student/attempts/{attempt.id}", headers={"X-Attempt-Token": token}
        )
        db.refresh(attempt)
        assert attempt.status is AttemptStatus.IN_PROGRESS

    def test_a_standard_quiz_is_never_marked_unverifiable(
        self, client: TestClient, db: Session
    ) -> None:
        """Nothing was promised, so nothing is missing."""
        attempt, token, _questions = full_attempt(db)
        response = client.get(
            f"/api/student/attempts/{attempt.id}", headers={"X-Attempt-Token": token}
        )
        assert response.json()["proctoring_status"] == "NOT_REQUIRED"

    def test_the_client_cannot_claim_a_lost_heartbeat(
        self, client: TestClient, db: Session
    ) -> None:
        """Server-generated events are refused on the student endpoint."""
        attempt, token, _ = proctored_attempt(db)
        response = client.post(
            f"/api/student/attempts/{attempt.id}/integrity-events",
            json={"event_type": "PROCTORING_HEARTBEAT_LOST"},
            headers={"X-Attempt-Token": token},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "SERVER_ONLY_EVENT"

    @pytest.mark.parametrize("event", ["PRECHECK_PASSED", "EVIDENCE_CAPTURED"])
    def test_other_server_markers_are_also_refused(
        self, client: TestClient, db: Session, event: str
    ) -> None:
        attempt, token, _ = proctored_attempt(db)
        response = client.post(
            f"/api/student/attempts/{attempt.id}/integrity-events",
            json={"event_type": event},
            headers={"X-Attempt-Token": token},
        )
        assert response.status_code == 400


class TestProctoringNeverTouchesTheResult:
    def test_a_heartbeat_cannot_change_a_score(
        self, client: TestClient, db: Session
    ) -> None:
        attempt, token, _ = proctored_attempt(db, max_integrity_violations=10)
        attempt.score = 7
        attempt.max_score = 10
        db.commit()
        beat(client, attempt, token, camera_active=False)
        db.refresh(attempt)
        assert float(attempt.score) == 7.0
        assert float(attempt.max_score) == 10.0

    def test_the_heartbeat_endpoint_is_refused_after_submission(
        self, client: TestClient, db: Session
    ) -> None:
        """A finalized attempt is not reopened by a late monitoring report."""
        attempt, token, _ = proctored_attempt(db)
        client.post(
            f"/api/student/attempts/{attempt.id}/submit",
            json={},
            headers={"X-Attempt-Token": token},
        )
        response = client.post(
            f"/api/student/attempts/{attempt.id}/proctoring/heartbeat",
            json=ALL_RUNNING,
            headers={"X-Attempt-Token": token},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "ATTEMPT_ALREADY_FINALIZED"

    def test_a_heartbeat_for_another_attempt_is_a_404(
        self, client: TestClient, db: Session
    ) -> None:
        attempt_a, token_a, _ = proctored_attempt(db, email="a@example.edu")
        attempt_b, _token_b, _ = proctored_attempt(db, email="b@example.edu")
        response = client.post(
            f"/api/student/attempts/{attempt_b.id}/proctoring/heartbeat",
            json=ALL_RUNNING,
            headers={"X-Attempt-Token": token_a},
        )
        assert response.status_code == 404


class TestTheSeverityTableStaysModest:
    """Weights are a judgement, so they are asserted rather than assumed."""

    def test_gaze_is_the_weakest_signal(self) -> None:
        """Looking away from a screen is what people do when they think."""
        assert integrity_policy.severity_for(IntegrityEventType.GAZE_AWAY_SUSTAINED) == 1

    def test_a_missing_face_is_not_worth_more_than_a_stopped_stream(self) -> None:
        face = integrity_policy.severity_for(IntegrityEventType.FACE_MISSING)
        stopped = integrity_policy.severity_for(IntegrityEventType.CAMERA_STOPPED)
        assert face < stopped

    def test_honest_self_reporting_is_free(self) -> None:
        assert integrity_policy.severity_for(IntegrityEventType.PROCTORING_UNAVAILABLE) == 0

    def test_every_proctoring_signal_has_a_weight(self) -> None:
        """An unweighted signal would silently count as zero forever."""
        for event_type in proctoring_policy.PROCTORING_SEVERITY:
            assert event_type in integrity_policy.SEVERITY

    def test_every_counted_proctoring_signal_has_a_repeat_window(self) -> None:
        """Otherwise a loop in the client could auto-submit an honest student."""
        missing = [
            event_type.value
            for event_type, weight in proctoring_policy.PROCTORING_SEVERITY.items()
            if weight > 0
            and event_type not in proctoring_policy.REPEAT_WINDOW_SECONDS
        ]
        assert not missing, f"counted signals with no debounce: {missing}"


class TestNoEventTypeNamesAVerdict:
    def test_the_vocabulary_describes_observations_only(self) -> None:
        """There is no CHEATING_DETECTED, and there should never be one."""
        banned = ("CHEAT", "GUILTY", "FRAUD", "MISCONDUCT", "SUSPICIOUS")
        offenders = [
            event_type.value
            for event_type in IntegrityEventType
            if any(word in event_type.value for word in banned)
        ]
        assert not offenders

    def test_the_review_vocabulary_has_no_cheating_verdict(self) -> None:
        from app.db.models import ProctoringReview

        values = {member.value for member in ProctoringReview}
        assert not any("CHEAT" in value for value in values)

    def test_proctoring_status_has_no_passed_value(self) -> None:
        """Monitoring is observed, impaired, or unverifiable. Never 'passed'."""
        values = {member.value for member in ProctoringStatus}
        assert not values & {"PASSED", "CLEAN", "OK", "VERIFIED"}
