"""Server-side exam start authorization.

These are the tests that matter most in Phase 5. The claim being defended is:

    A proctored exam cannot be started by the browser. It is started by the
    server, after the server has decided the checks passed.

Each class below attacks that claim from a different direction: skip the checks
entirely, fake the verdict, reuse an authorization, race two tabs, or wait for
the policy to change underneath a stale approval.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.models import (
    Attempt,
    PrecheckSession,
    PrecheckStatus,
    ProctoringMode,
    ProctoringStatus,
    QuizStatus,
)
from tests.factories import make_family, make_quiz, make_user, make_variant

READY_OBSERVATIONS = {
    "camera_active": True,
    "fullscreen_active": True,
    "face_detected": True,
    "consent_accepted": True,
}

CAPABLE_BROWSER = {
    "camera": True,
    "fullscreen": True,
    "face_detection": True,
    "secure_context": True,
    "is_mobile": False,
}


def proctored_quiz(db: Session, **overrides) -> object:  # type: ignore[no-untyped-def]
    """A published, PROCTORED quiz with a paper it can actually serve."""
    owner = make_user(db, email=f"owner-{uuid.uuid4().hex[:8]}@example.edu")
    quiz = make_quiz(
        db,
        owner,
        question_count=2,
        status=QuizStatus.PUBLISHED,
        public_code=overrides.pop("public_code", "PRCTRD22"),
        proctoring_mode=overrides.pop("proctoring_mode", ProctoringMode.PROCTORED),
        **overrides,
    )
    for position in range(2):
        make_variant(db, make_family(db, quiz, position=position))
    return quiz


def open_precheck(client: TestClient, code: str, email: str = "s@example.edu") -> dict:
    response = client.post(
        f"/api/public/quizzes/{code}/precheck",
        json={"student_name": "Student One", "student_email": email},
    )
    assert response.status_code == 201, response.text
    return response.json()


def headers(token: str) -> dict[str, str]:
    return {"X-Precheck-Token": token}


def walk_to_ready(client: TestClient, code: str, email: str = "s@example.edu") -> dict:
    """Complete the happy path up to, but not including, the start call."""
    session = open_precheck(client, code, email)
    token = session["precheck_token"]
    client.post(
        f"/api/public/precheck/{session['precheck_id']}/capabilities",
        json=CAPABLE_BROWSER,
        headers=headers(token),
    )
    client.post(
        f"/api/public/precheck/{session['precheck_id']}/readiness",
        json=READY_OBSERVATIONS,
        headers=headers(token),
    )
    return session


class TestTheChecksCannotBeSkipped:
    """The Phase 3 start route is the obvious bypass. It is closed."""

    def test_the_direct_start_route_refuses_a_proctored_quiz(
        self, client: TestClient, db: Session
    ) -> None:
        """This is the whole bypass prevention, stated as a test."""
        proctored_quiz(db)
        response = client.post(
            "/api/public/quizzes/PRCTRD22/attempts",
            json={"student_name": "Student One", "student_email": "s@example.edu"},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "PRECHECK_REQUIRED"

    def test_no_attempt_row_is_created_by_the_refused_call(
        self, client: TestClient, db: Session
    ) -> None:
        quiz = proctored_quiz(db)
        client.post(
            "/api/public/quizzes/PRCTRD22/attempts",
            json={"student_name": "Student One", "student_email": "s@example.edu"},
        )
        assert db.query(Attempt).filter(Attempt.quiz_id == quiz.id).count() == 0

    def test_a_standard_quiz_still_starts_directly(
        self, client: TestClient, db: Session
    ) -> None:
        """Phase 3 behaviour is untouched for quizzes that never opted in."""
        proctored_quiz(db, proctoring_mode=ProctoringMode.STANDARD, public_code="STNDRD22")
        response = client.post(
            "/api/public/quizzes/STNDRD22/attempts",
            json={"student_name": "Student One", "student_email": "s@example.edu"},
        )
        assert response.status_code == 201, response.text
        assert response.json()["proctoring_mode"] == "STANDARD"

    def test_starting_without_submitting_readiness_is_refused(
        self, client: TestClient, db: Session
    ) -> None:
        """Opening a session is not the same as passing it."""
        proctored_quiz(db)
        session = open_precheck(client, "PRCTRD22")
        response = client.post(
            f"/api/public/precheck/{session['precheck_id']}/start",
            json={},
            headers=headers(session["precheck_token"]),
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "PRECHECK_NOT_READY"

    def test_starting_after_a_failed_readiness_is_refused(
        self, client: TestClient, db: Session
    ) -> None:
        proctored_quiz(db)
        session = open_precheck(client, "PRCTRD22")
        token = session["precheck_token"]
        client.post(
            f"/api/public/precheck/{session['precheck_id']}/capabilities",
            json=CAPABLE_BROWSER,
            headers=headers(token),
        )
        # Camera off: the exam requires it.
        client.post(
            f"/api/public/precheck/{session['precheck_id']}/readiness",
            json={**READY_OBSERVATIONS, "camera_active": False},
            headers=headers(token),
        )
        response = client.post(
            f"/api/public/precheck/{session['precheck_id']}/start",
            json={},
            headers=headers(token),
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "PRECHECK_NOT_READY"


class TestTheBrowserCannotAssertAVerdict:
    """The client submits observations. It has no way to submit a conclusion."""

    @pytest.mark.parametrize(
        "forbidden",
        [
            {"ready": True},
            {"status": "READY"},
            {"checks_passed": True},
            {"severity": 0},
            {"violation_count": 0},
            {"suspicion_score": 0.0},
            {"verdict": "CLEAN"},
        ],
    )
    def test_a_verdict_field_is_rejected_not_ignored(
        self, client: TestClient, db: Session, forbidden: dict
    ) -> None:
        """422, so a client learns the server decides — rather than believing
        it got away with it."""
        proctored_quiz(db)
        session = open_precheck(client, "PRCTRD22")
        token = session["precheck_token"]
        client.post(
            f"/api/public/precheck/{session['precheck_id']}/capabilities",
            json=CAPABLE_BROWSER,
            headers=headers(token),
        )
        response = client.post(
            f"/api/public/precheck/{session['precheck_id']}/readiness",
            json={**READY_OBSERVATIONS, **forbidden},
            headers=headers(token),
        )
        assert response.status_code == 422

    def test_the_readiness_model_has_no_verdict_field(self) -> None:
        """Belt and braces: the shape itself cannot express a verdict."""
        from app.schemas.proctoring import ReadinessReport

        fields = set(ReadinessReport.model_fields)
        assert not fields & {
            "ready",
            "status",
            "verdict",
            "severity",
            "violation_count",
            "suspicion_score",
            "passed",
        }

    def test_claiming_capability_does_not_substitute_for_the_signal(
        self, client: TestClient, db: Session
    ) -> None:
        """A browser can lie about what it *can* do and gain nothing.

        Capabilities gate the start; they do not satisfy it. The actual
        observations still have to arrive.
        """
        proctored_quiz(db)
        session = open_precheck(client, "PRCTRD22")
        token = session["precheck_token"]
        client.post(
            f"/api/public/precheck/{session['precheck_id']}/capabilities",
            json={**CAPABLE_BROWSER, "camera": True, "face_detection": True},
            headers=headers(token),
        )
        response = client.post(
            f"/api/public/precheck/{session['precheck_id']}/readiness",
            json={"camera_active": False, "consent_accepted": True},
            headers=headers(token),
        )
        assert response.json()["ready"] is False

    def test_honest_impairment_blocks_rather_than_downgrades(
        self, client: TestClient, db: Session
    ) -> None:
        """A client that admits it cannot monitor is believed, and refused.

        This is the opposite of a downgrade: honesty blocks, it does not wave
        the student through into an exam nobody is watching.
        """
        proctored_quiz(db)
        session = open_precheck(client, "PRCTRD22")
        token = session["precheck_token"]
        client.post(
            f"/api/public/precheck/{session['precheck_id']}/capabilities",
            json=CAPABLE_BROWSER,
            headers=headers(token),
        )
        response = client.post(
            f"/api/public/precheck/{session['precheck_id']}/readiness",
            json={**READY_OBSERVATIONS, "monitoring_impaired": True},
            headers=headers(token),
        )
        assert response.json()["ready"] is False
        assert response.json()["status"] == "FAILED"


class TestCapabilityGatingBlocksRatherThanDowngrades:
    def test_a_browser_without_face_detection_is_blocked(
        self, client: TestClient, db: Session
    ) -> None:
        proctored_quiz(db)
        session = open_precheck(client, "PRCTRD22")
        response = client.post(
            f"/api/public/precheck/{session['precheck_id']}/capabilities",
            json={**CAPABLE_BROWSER, "face_detection": False},
            headers=headers(session["precheck_token"]),
        )
        body = response.json()
        assert body["satisfiable"] is False
        assert "face_detection" in body["missing"]
        assert body["status"] == "BLOCKED"

    def test_a_blocked_session_cannot_proceed_to_readiness(
        self, client: TestClient, db: Session
    ) -> None:
        """The blocked state is enforced, not merely reported."""
        proctored_quiz(db)
        session = open_precheck(client, "PRCTRD22")
        token = session["precheck_token"]
        client.post(
            f"/api/public/precheck/{session['precheck_id']}/capabilities",
            json={**CAPABLE_BROWSER, "camera": False},
            headers=headers(token),
        )
        response = client.post(
            f"/api/public/precheck/{session['precheck_id']}/readiness",
            json=READY_OBSERVATIONS,
            headers=headers(token),
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "PRECHECK_BLOCKED"

    def test_a_mobile_browser_is_blocked_when_the_policy_says_desktop(
        self, client: TestClient, db: Session
    ) -> None:
        proctored_quiz(db)
        session = open_precheck(client, "PRCTRD22")
        response = client.post(
            f"/api/public/precheck/{session['precheck_id']}/capabilities",
            json={**CAPABLE_BROWSER, "is_mobile": True},
            headers=headers(session["precheck_token"]),
        )
        assert "desktop_browser" in response.json()["missing"]

    def test_an_insecure_context_is_blocked(
        self, client: TestClient, db: Session
    ) -> None:
        """getUserMedia needs https; without it nothing below is meaningful."""
        proctored_quiz(db)
        session = open_precheck(client, "PRCTRD22")
        response = client.post(
            f"/api/public/precheck/{session['precheck_id']}/capabilities",
            json={**CAPABLE_BROWSER, "secure_context": False},
            headers=headers(session["precheck_token"]),
        )
        assert "secure_context" in response.json()["missing"]


class TestTheTimerStartsAtAuthorization:
    def test_no_attempt_exists_before_the_start_call(
        self, client: TestClient, db: Session
    ) -> None:
        """The point of the whole design: no clock runs during the checks."""
        quiz = proctored_quiz(db)
        walk_to_ready(client, "PRCTRD22")
        assert db.query(Attempt).filter(Attempt.quiz_id == quiz.id).count() == 0

    def test_started_at_is_assigned_by_the_server_at_start(
        self, client: TestClient, db: Session
    ) -> None:
        proctored_quiz(db)
        session = walk_to_ready(client, "PRCTRD22")
        response = client.post(
            f"/api/public/precheck/{session['precheck_id']}/start",
            json={},
            headers=headers(session["precheck_token"]),
        )
        assert response.status_code == 201, response.text
        body = response.json()
        # A full-length exam, not one shortened by time spent on permissions.
        assert body["seconds_remaining"] > 1190
        assert body["proctoring_mode"] == "PROCTORED"
        assert body["heartbeat_interval_seconds"] > 0

    def test_the_attempt_records_how_it_was_authorized(
        self, client: TestClient, db: Session
    ) -> None:
        """The audit trail says a server-side authorization preceded the exam."""
        proctored_quiz(db)
        session = walk_to_ready(client, "PRCTRD22")
        response = client.post(
            f"/api/public/precheck/{session['precheck_id']}/start",
            json={},
            headers=headers(session["precheck_token"]),
        )
        attempt = db.get(Attempt, uuid.UUID(response.json()["attempt_id"]))
        assert attempt is not None
        assert attempt.precheck_session_id == uuid.UUID(session["precheck_id"])
        assert attempt.proctoring_mode is ProctoringMode.PROCTORED
        assert attempt.proctoring_status is ProctoringStatus.ACTIVE
        assert any(
            event.event_type.value == "PRECHECK_PASSED"
            for event in attempt.integrity_events
        )


class TestAnAuthorizationIsSingleUse:
    def test_a_second_start_is_refused(self, client: TestClient, db: Session) -> None:
        proctored_quiz(db)
        session = walk_to_ready(client, "PRCTRD22")
        first = client.post(
            f"/api/public/precheck/{session['precheck_id']}/start",
            json={},
            headers=headers(session["precheck_token"]),
        )
        assert first.status_code == 201
        second = client.post(
            f"/api/public/precheck/{session['precheck_id']}/start",
            json={},
            headers=headers(session["precheck_token"]),
        )
        assert second.status_code == 409
        assert second.json()["error"]["code"] == "PRECHECK_ALREADY_USED"

    def test_only_one_attempt_exists_after_a_replay(
        self, client: TestClient, db: Session
    ) -> None:
        quiz = proctored_quiz(db)
        session = walk_to_ready(client, "PRCTRD22")
        for _ in range(3):
            client.post(
                f"/api/public/precheck/{session['precheck_id']}/start",
                json={},
                headers=headers(session["precheck_token"]),
            )
        assert db.query(Attempt).filter(Attempt.quiz_id == quiz.id).count() == 1

    def test_a_fresh_session_cannot_produce_a_second_attempt(
        self, client: TestClient, db: Session
    ) -> None:
        """The one-attempt rule still belongs to the attempts table.

        A student who opens a new precheck after finishing one is stopped by
        the unique constraint, not by anything in the precheck layer.
        """
        proctored_quiz(db)
        first = walk_to_ready(client, "PRCTRD22", email="dup@example.edu")
        client.post(
            f"/api/public/precheck/{first['precheck_id']}/start",
            json={},
            headers=headers(first["precheck_token"]),
        )
        response = client.post(
            "/api/public/quizzes/PRCTRD22/precheck",
            json={"student_name": "Student One", "student_email": "dup@example.edu"},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "ATTEMPT_ALREADY_EXISTS"


class TestStaleAuthorizationsDoNotSurvivePolicyChanges:
    def test_a_tightened_policy_invalidates_a_stored_ready(
        self, client: TestClient, db: Session
    ) -> None:
        """READY is a record of a past decision, not a licence.

        The start endpoint re-evaluates rather than trusting the stored status,
        so a policy tightened while a student sat on the readiness screen is
        the one that applies.
        """
        quiz = proctored_quiz(db)
        session = walk_to_ready(client, "PRCTRD22")

        # The teacher now also requires a microphone.
        quiz.proctoring_config_json = {"require_camera": True, "require_microphone": True}
        db.commit()

        response = client.post(
            f"/api/public/precheck/{session['precheck_id']}/start",
            json={},
            headers=headers(session["precheck_token"]),
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "PRECHECK_NOT_READY"

    def test_a_changed_mode_invalidates_the_session(
        self, client: TestClient, db: Session
    ) -> None:
        """A PROCTORED check must not authorize a LOCKDOWN exam."""
        quiz = proctored_quiz(db)
        session = walk_to_ready(client, "PRCTRD22")
        quiz.proctoring_mode = ProctoringMode.LOCKDOWN
        db.commit()

        response = client.post(
            f"/api/public/precheck/{session['precheck_id']}/start",
            json={},
            headers=headers(session["precheck_token"]),
        )
        assert response.status_code == 409

    def test_a_forged_ready_status_still_does_not_start_an_exam(
        self, client: TestClient, db: Session
    ) -> None:
        """Defence in depth against a bug rather than an attacker.

        Even if something writes READY without the observations to justify it,
        the re-evaluation at start refuses.
        """
        proctored_quiz(db)
        opened = open_precheck(client, "PRCTRD22")
        row = db.get(PrecheckSession, uuid.UUID(opened["precheck_id"]))
        assert row is not None
        row.status = PrecheckStatus.READY  # observations_json stays empty
        db.commit()

        response = client.post(
            f"/api/public/precheck/{opened['precheck_id']}/start",
            json={},
            headers=headers(opened["precheck_token"]),
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "PRECHECK_NOT_READY"


class TestSessionTokensDoNotLeak:
    """Same rules as attempt tokens; see attempt_access."""

    def test_a_wrong_token_is_a_404(self, client: TestClient, db: Session) -> None:
        proctored_quiz(db)
        session = open_precheck(client, "PRCTRD22")
        response = client.post(
            f"/api/public/precheck/{session['precheck_id']}/capabilities",
            json=CAPABLE_BROWSER,
            headers=headers("not-the-token"),
        )
        assert response.status_code == 404

    def test_another_students_session_id_is_a_404_not_a_403(
        self, client: TestClient, db: Session
    ) -> None:
        """A valid token plus somebody else's id must not be an oracle."""
        proctored_quiz(db)
        mine = open_precheck(client, "PRCTRD22", email="me@example.edu")
        theirs = open_precheck(client, "PRCTRD22", email="them@example.edu")
        response = client.post(
            f"/api/public/precheck/{theirs['precheck_id']}/capabilities",
            json=CAPABLE_BROWSER,
            headers=headers(mine["precheck_token"]),
        )
        assert response.status_code == 404

    def test_a_nonexistent_id_looks_identical(
        self, client: TestClient, db: Session
    ) -> None:
        proctored_quiz(db)
        mine = open_precheck(client, "PRCTRD22")
        real = client.post(
            f"/api/public/precheck/{uuid.uuid4()}/capabilities",
            json=CAPABLE_BROWSER,
            headers=headers(mine["precheck_token"]),
        )
        assert real.status_code == 404
        assert real.json()["error"]["code"] == "PRECHECK_NOT_FOUND"

    def test_a_malformed_id_is_also_a_404(self, client: TestClient, db: Session) -> None:
        proctored_quiz(db)
        mine = open_precheck(client, "PRCTRD22")
        response = client.post(
            "/api/public/precheck/not-a-uuid/capabilities",
            json=CAPABLE_BROWSER,
            headers=headers(mine["precheck_token"]),
        )
        assert response.status_code == 404

    def test_a_missing_token_header_is_refused(
        self, client: TestClient, db: Session
    ) -> None:
        proctored_quiz(db)
        session = open_precheck(client, "PRCTRD22")
        response = client.post(
            f"/api/public/precheck/{session['precheck_id']}/capabilities",
            json=CAPABLE_BROWSER,
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "MISSING_PRECHECK_TOKEN"

    def test_only_the_hash_is_stored(self, client: TestClient, db: Session) -> None:
        proctored_quiz(db)
        session = open_precheck(client, "PRCTRD22")
        row = db.get(PrecheckSession, uuid.UUID(session["precheck_id"]))
        assert row is not None
        assert row.session_token_hash != session["precheck_token"]
        assert len(row.session_token_hash) == 64


class TestLockdownIsRefusedNotDowngraded:
    def test_opening_a_lockdown_precheck_is_refused(
        self, client: TestClient, db: Session
    ) -> None:
        """Refused before the student hands over camera access, not after."""
        proctored_quiz(db, proctoring_mode=ProctoringMode.LOCKDOWN, public_code="KCKDWN22")
        response = client.post(
            "/api/public/quizzes/KCKDWN22/precheck",
            json={"student_name": "Student One", "student_email": "s@example.edu"},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "LOCKDOWN_CLIENT_REQUIRED"

    def test_a_lockdown_quiz_does_not_fall_back_to_the_direct_route(
        self, client: TestClient, db: Session
    ) -> None:
        """Both doors are shut, so there is no accidental STANDARD exam."""
        proctored_quiz(db, proctoring_mode=ProctoringMode.LOCKDOWN, public_code="KCKDWN33")
        response = client.post(
            "/api/public/quizzes/KCKDWN33/attempts",
            json={"student_name": "Student One", "student_email": "s@example.edu"},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "PRECHECK_REQUIRED"

    def test_no_attempt_is_ever_created_for_a_lockdown_quiz(
        self, client: TestClient, db: Session
    ) -> None:
        quiz = proctored_quiz(
            db, proctoring_mode=ProctoringMode.LOCKDOWN, public_code="KCKDWN44"
        )
        client.post(
            "/api/public/quizzes/KCKDWN44/precheck",
            json={"student_name": "S", "student_email": "s@example.edu"},
        )
        client.post(
            "/api/public/quizzes/KCKDWN44/attempts",
            json={"student_name": "S", "student_email": "s@example.edu"},
        )
        assert db.query(Attempt).filter(Attempt.quiz_id == quiz.id).count() == 0


class TestTheStudentIsToldWhatIsMonitored:
    def test_the_requirements_are_published_before_permissions_are_asked(
        self, client: TestClient, db: Session
    ) -> None:
        proctored_quiz(db)
        body = open_precheck(client, "PRCTRD22")
        requirements = body["requirements"]
        assert requirements["camera"] is True
        assert requirements["face_presence"] is True
        assert requirements["consent"] is True
        assert requirements["evidence_retention_days"] > 0

    def test_the_monitoring_notice_states_the_limits(
        self, client: TestClient, db: Session
    ) -> None:
        """The student reads what monitoring cannot see, not just what it can."""
        proctored_quiz(db)
        body = open_precheck(client, "PRCTRD22")
        notice = " ".join(body["monitoring_notice"]).lower()
        assert "cannot" in notice
        assert notice

    def test_the_notice_never_claims_cheating_is_impossible(
        self, client: TestClient, db: Session
    ) -> None:
        proctored_quiz(db)
        body = open_precheck(client, "PRCTRD22")
        text = " ".join(body["monitoring_notice"]).lower()
        for banned in ("impossible", "cannot cheat", "guaranteed", "foolproof"):
            assert banned not in text
