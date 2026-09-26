"""Evidence handling, privacy, and teacher review.

The claims under test:

* evidence is private — no public bucket, no URL in a student response, no path
  in a log, and a signed URL only after an ownership check;
* nothing is stored without a retention date;
* the review surface reports signals and limits, never a verdict about a person;
* a teacher cannot read another teacher's proctoring record, and finds out by
  404 rather than 403.
"""

from __future__ import annotations

import io
import json
import logging
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.db.models import (
    EvidenceKind,
    EvidenceSource,
    IntegrityEventType,
    ProctoringEvidence,
    ProctoringMode,
    ProctoringReview,
    ProctoringStatus,
    Quiz,
)
from app.services import evidence_service, integrity_policy, proctoring_service
from app.services.proctoring_policy import policy_for
from tests.factories import full_attempt, make_family, make_quiz, make_user, make_variant

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200


def proctored(db: Session, **kwargs):  # type: ignore[no-untyped-def]
    attempt, token, _questions = full_attempt(db, **kwargs)
    quiz = db.get(Quiz, attempt.quiz_id)
    assert quiz is not None
    quiz.proctoring_mode = ProctoringMode.PROCTORED
    quiz.proctoring_config_json = {"require_camera": True, "store_evidence": True}
    quiz.max_integrity_violations = 10
    attempt.proctoring_mode = ProctoringMode.PROCTORED
    attempt.proctoring_status = ProctoringStatus.ACTIVE
    attempt.last_heartbeat_at = utcnow()
    db.commit()
    db.refresh(attempt)
    return attempt, token, quiz


def upload(client: TestClient, attempt, token: str, *, kind: str = "EVENT_SNAPSHOT",
           content: bytes = PNG, content_type: str = "image/png"):  # type: ignore[no-untyped-def]
    return client.post(
        f"/api/student/attempts/{attempt.id}/proctoring/evidence",
        data={"kind": kind},
        files={"file": ("frame.png", io.BytesIO(content), content_type)},
        headers={"X-Attempt-Token": token},
    )


class TestEvidenceIsBounded:
    def test_a_snapshot_is_accepted(self, client: TestClient, db: Session) -> None:
        attempt, token, _ = proctored(db)
        response = upload(client, attempt, token)
        assert response.status_code == 200, response.text
        assert response.json()["stored"] is True

    def test_an_oversized_file_is_refused(self, client: TestClient, db: Session) -> None:
        """The endpoint is authenticated by an attempt token alone, so it must
        not double as free object storage.

        413 from the global body-size middleware and 400 from the service check
        are both correct refusals; which fires depends on whether the body
        crosses MAX_REQUEST_BYTES first. What matters is that nothing is stored.
        """
        attempt, token, _ = proctored(db)
        limit = evidence_service.max_bytes_for(EvidenceKind.EVENT_SNAPSHOT)
        huge = b"\x89PNG\r\n\x1a\n" + b"\x00" * (limit + 10)
        response = upload(client, attempt, token, content=huge)
        assert response.status_code in (400, 413)
        assert db.query(ProctoringEvidence).count() == 0

    def test_the_declared_limit_never_exceeds_the_global_body_cap(self) -> None:
        """Otherwise the documented limit would be unreachable in practice."""
        from app.core.config import settings

        for kind in EvidenceKind:
            assert evidence_service.max_bytes_for(kind) <= settings.max_request_bytes

    def test_a_wrong_content_type_is_refused(
        self, client: TestClient, db: Session
    ) -> None:
        attempt, token, _ = proctored(db)
        response = upload(
            client, attempt, token, content=b"#!/bin/sh\n", content_type="application/x-sh"
        )
        assert response.status_code == 400

    def test_a_video_type_is_refused_for_a_snapshot_kind(
        self, client: TestClient, db: Session
    ) -> None:
        attempt, token, _ = proctored(db)
        response = upload(client, attempt, token, content_type="video/webm")
        assert response.status_code == 400

    def test_an_unknown_kind_is_refused(self, client: TestClient, db: Session) -> None:
        attempt, token, _ = proctored(db)
        response = upload(client, attempt, token, kind="EVERYTHING")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "EVIDENCE_KIND_INVALID"

    def test_an_empty_file_is_refused(self, client: TestClient, db: Session) -> None:
        attempt, token, _ = proctored(db)
        response = upload(client, attempt, token, content=b"")
        assert response.status_code == 400


class TestEvidenceIsPrivate:
    def test_the_student_response_carries_no_url(
        self, client: TestClient, db: Session
    ) -> None:
        """A student uploads evidence about themselves; they do not read it back."""
        attempt, token, _ = proctored(db)
        body = upload(client, attempt, token).json()
        serialized = json.dumps(body).lower()
        assert "http" not in serialized
        assert "object_path" not in serialized
        assert "bucket" not in serialized

    def test_the_upload_response_model_has_no_url_field(self) -> None:
        from app.schemas.proctoring import EvidenceUploadResponse

        fields = set(EvidenceUploadResponse.model_fields)
        assert not fields & {"url", "download_url", "object_path", "bucket", "signed_url"}

    def test_the_object_path_is_not_logged(
        self, client: TestClient, db: Session, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The path is the only secret protecting the object."""
        attempt, token, _ = proctored(db)
        with caplog.at_level(logging.INFO):
            upload(client, attempt, token)
        stored = db.query(ProctoringEvidence).one()
        assert stored.object_path not in caplog.text

    def test_the_path_is_namespaced_and_unguessable(self, db: Session) -> None:
        attempt_id = uuid.uuid4()
        first = evidence_service.object_path(attempt_id, EvidenceKind.EVENT_SNAPSHOT, "png")
        second = evidence_service.object_path(attempt_id, EvidenceKind.EVENT_SNAPSHOT, "png")
        assert first.startswith(f"attempts/{attempt_id}/")
        assert first != second

    def test_evidence_path_and_signed_url_are_redaction_keys(self) -> None:
        from app.core.logging import _REDACT_KEYS

        assert "object_path" in _REDACT_KEYS
        assert "signed_url" in _REDACT_KEYS


class TestRetentionIsSetAtCapture:
    def test_every_artefact_has_a_deletion_date(
        self, client: TestClient, db: Session
    ) -> None:
        attempt, token, _ = proctored(db)
        upload(client, attempt, token)
        stored = db.query(ProctoringEvidence).one()
        assert stored.retention_expires_at is not None
        assert stored.retention_expires_at > stored.captured_at

    def test_the_date_follows_the_quiz_policy(
        self, client: TestClient, db: Session
    ) -> None:
        attempt, token, quiz = proctored(db)
        quiz.proctoring_config_json = {
            "require_camera": True,
            "store_evidence": True,
            "evidence_retention_days": 7,
        }
        db.commit()
        upload(client, attempt, token)
        stored = db.query(ProctoringEvidence).one()
        days = (stored.retention_expires_at - stored.captured_at).days
        assert days == 7

    def test_expired_evidence_is_findable_for_purging(
        self, client: TestClient, db: Session
    ) -> None:
        attempt, token, _ = proctored(db)
        upload(client, attempt, token)
        stored = db.query(ProctoringEvidence).one()
        assert evidence_service.expired_evidence(db) == []

        stored.retention_expires_at = utcnow().replace(year=utcnow().year - 1)
        db.commit()
        assert len(evidence_service.expired_evidence(db)) == 1

    def test_storage_is_skipped_when_the_teacher_turned_it_off(
        self, client: TestClient, db: Session
    ) -> None:
        attempt, token, quiz = proctored(db)
        quiz.proctoring_config_json = {"require_camera": True, "store_evidence": False}
        db.commit()
        body = upload(client, attempt, token).json()
        assert body["stored"] is False
        assert body["reason"] == "EVIDENCE_DISABLED"
        assert db.query(ProctoringEvidence).count() == 0


class TestAStorageFailureNeverCostsAnExam:
    @pytest.mark.anyio
    async def test_an_upload_failure_returns_rather_than_raises(
        self, db: Session
    ) -> None:
        from app.storage.base import StorageProvider, StorageUnavailable

        class BrokenStorage(StorageProvider):
            name = "broken"

            async def upload(self, **kwargs):  # type: ignore[no-untyped-def]
                raise StorageUnavailable("bucket unreachable")

            async def signed_url(self, **kwargs):  # type: ignore[no-untyped-def]
                raise StorageUnavailable("bucket unreachable")

            async def delete(self, **kwargs):  # type: ignore[no-untyped-def]
                raise StorageUnavailable("bucket unreachable")

        attempt, _token, _q = full_attempt(db)
        quiz = db.get(Quiz, attempt.quiz_id)
        assert quiz is not None
        captured = await evidence_service.capture(
            db,
            attempt,
            policy_for(quiz),
            kind=EvidenceKind.EVENT_SNAPSHOT,
            content=PNG,
            content_type="image/png",
            provider=BrokenStorage(),
        )
        assert captured.stored is False
        assert captured.reason == "STORAGE_UNAVAILABLE"
        assert db.query(ProctoringEvidence).count() == 0

    def test_a_storage_failure_does_not_end_the_attempt(
        self, db: Session
    ) -> None:
        attempt, _token, _q = proctored(db)[0], None, None  # type: ignore[assignment]
        assert attempt.status.value == "IN_PROGRESS"


class TestEvidenceIsLabelledAsAClaim:
    def test_a_student_upload_is_recorded_as_a_client_claim(
        self, client: TestClient, db: Session
    ) -> None:
        """An artefact from the student's own browser is a claim about the
        room, not an observation by the server. Labelling it accurately is what
        keeps a later review honest."""
        attempt, token, _ = proctored(db)
        upload(client, attempt, token)
        stored = db.query(ProctoringEvidence).one()
        assert stored.source is EvidenceSource.CLIENT_CLAIM


class TestTeacherReview:
    def _reviewable(self, client: TestClient, db: Session, teacher_headers: dict):  # type: ignore[no-untyped-def]
        """An attempt on a quiz owned by the authenticated teacher."""
        from app.db.models import User

        me = db.query(User).filter(
            User.email == "teacher.a@example.edu"
        ).one()
        quiz = make_quiz(db, me, question_count=1, public_code=None)
        quiz.proctoring_mode = ProctoringMode.PROCTORED
        quiz.proctoring_config_json = {"require_camera": True}
        quiz.max_integrity_violations = 10
        variant = make_variant(db, make_family(db, quiz, position=0))
        from tests.factories import make_attempt, make_paper

        attempt, token = make_attempt(db, quiz)
        attempt.proctoring_mode = ProctoringMode.PROCTORED
        attempt.proctoring_status = ProctoringStatus.ACTIVE
        make_paper(db, attempt, [variant])
        db.commit()
        return attempt, token, quiz

    def test_a_teacher_reads_their_own_attempt(
        self, client: TestClient, db: Session, teacher_a: dict
    ) -> None:
        attempt, _token, _quiz = self._reviewable(client, db, teacher_a)
        response = client.get(
            f"/api/attempts/{attempt.id}/proctoring", headers=teacher_a
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["attempt_id"] == str(attempt.id)
        assert "summary" in body

    def test_another_teacher_gets_a_404_not_a_403(
        self, client: TestClient, db: Session, teacher_a: dict, teacher_b: dict
    ) -> None:
        """Ownership is a predicate in the query, so nothing leaks — not even
        that the attempt exists."""
        attempt, _token, _quiz = self._reviewable(client, db, teacher_a)
        response = client.get(
            f"/api/attempts/{attempt.id}/proctoring", headers=teacher_b
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "ATTEMPT_NOT_FOUND"

    def test_an_unauthenticated_read_is_refused(
        self, client: TestClient, db: Session, teacher_a: dict
    ) -> None:
        attempt, _token, _quiz = self._reviewable(client, db, teacher_a)
        assert client.get(f"/api/attempts/{attempt.id}/proctoring").status_code == 401

    def test_a_student_attempt_token_cannot_read_the_review(
        self, client: TestClient, db: Session, teacher_a: dict
    ) -> None:
        """The student's own token grants nothing on the teacher surface."""
        attempt, token, _quiz = self._reviewable(client, db, teacher_a)
        response = client.get(
            f"/api/attempts/{attempt.id}/proctoring",
            headers={"X-Attempt-Token": token},
        )
        assert response.status_code == 401

    def test_the_summary_states_the_limits_of_the_monitoring(
        self, client: TestClient, db: Session, teacher_a: dict
    ) -> None:
        """The caveats sit next to the numbers, not in a help page nobody reads."""
        attempt, _token, _quiz = self._reviewable(client, db, teacher_a)
        body = client.get(
            f"/api/attempts/{attempt.id}/proctoring", headers=teacher_a
        ).json()
        caveats = " ".join(body["summary"]["caveats"]).lower()
        assert "browser" in caveats
        assert "cannot see the room" in caveats
        assert "signals, not findings" in caveats

    def test_the_capability_inventory_admits_no_os_lockdown(
        self, client: TestClient, db: Session, teacher_a: dict
    ) -> None:
        attempt, _token, _quiz = self._reviewable(client, db, teacher_a)
        body = client.get(
            f"/api/attempts/{attempt.id}/proctoring", headers=teacher_a
        ).json()
        capabilities = body["monitoring_capabilities"]
        assert capabilities["provides_os_lockdown"] is False
        assert capabilities["can_block_other_applications"] is False

    def test_the_summary_never_uses_the_word_cheating(
        self, client: TestClient, db: Session, teacher_a: dict
    ) -> None:
        attempt, _token, _quiz = self._reviewable(client, db, teacher_a)
        body = client.get(
            f"/api/attempts/{attempt.id}/proctoring", headers=teacher_a
        ).json()
        text = json.dumps(body["summary"]).lower()
        for banned in ("cheat", "guilty", "misconduct", "suspicion score"):
            assert banned not in text

    def test_a_clean_attempt_reports_no_signals(
        self, client: TestClient, db: Session, teacher_a: dict
    ) -> None:
        attempt, _token, _quiz = self._reviewable(client, db, teacher_a)
        body = client.get(
            f"/api/attempts/{attempt.id}/proctoring", headers=teacher_a
        ).json()
        assert body["summary"]["outcome"] == "NO_SIGNALS"

    def test_a_degraded_attempt_reports_incomplete_monitoring(
        self, client: TestClient, db: Session, teacher_a: dict
    ) -> None:
        """The honest outcome, and the one a naive build would call clean."""
        attempt, _token, _quiz = self._reviewable(client, db, teacher_a)
        attempt.proctoring_status = ProctoringStatus.UNVERIFIABLE
        db.commit()
        body = client.get(
            f"/api/attempts/{attempt.id}/proctoring", headers=teacher_a
        ).json()
        assert body["summary"]["outcome"] == "MONITORING_INCOMPLETE"
        caveats = " ".join(body["summary"]["caveats"]).lower()
        assert "not evidence of misconduct" in caveats

    def test_exceeding_the_threshold_recommends_a_human_look(
        self, client: TestClient, db: Session, teacher_a: dict
    ) -> None:
        """The strongest thing the platform will say is 'someone should look'."""
        attempt, _token, quiz = self._reviewable(client, db, teacher_a)
        quiz.max_integrity_violations = 1
        attempt.integrity_violation_count = 5
        db.commit()
        body = client.get(
            f"/api/attempts/{attempt.id}/proctoring", headers=teacher_a
        ).json()
        assert body["summary"]["outcome"] == "REVIEW_RECOMMENDED"

    def test_the_timeline_is_returned(
        self, client: TestClient, db: Session, teacher_a: dict
    ) -> None:
        attempt, _token, quiz = self._reviewable(client, db, teacher_a)
        integrity_policy.record_event(
            db, attempt, quiz, event_type=IntegrityEventType.FACE_MISSING
        )
        db.commit()
        body = client.get(
            f"/api/attempts/{attempt.id}/proctoring", headers=teacher_a
        ).json()
        assert any(e["event_type"] == "FACE_MISSING" for e in body["events"])


class TestRecordingAReviewDecision:
    def _attempt(self, db: Session):  # type: ignore[no-untyped-def]
        from app.db.models import User

        me = db.query(User).filter(User.email == "teacher.a@example.edu").one()
        quiz = make_quiz(db, me, question_count=1, public_code=None)
        variant = make_variant(db, make_family(db, quiz, position=0))
        from tests.factories import make_attempt, make_paper

        attempt, _token = make_attempt(db, quiz)
        make_paper(db, attempt, [variant])
        db.commit()
        return attempt

    def test_a_decision_is_attributed_and_timestamped(
        self, client: TestClient, db: Session, teacher_a: dict
    ) -> None:
        attempt = self._attempt(db)
        response = client.post(
            f"/api/attempts/{attempt.id}/proctoring/review",
            json={"decision": "REVIEWED_NO_ACTION", "note": "Sibling walked past."},
            headers=teacher_a,
        )
        assert response.status_code == 200, response.text
        db.refresh(attempt)
        assert attempt.proctoring_review is ProctoringReview.REVIEWED_NO_ACTION
        assert attempt.reviewed_at is not None
        assert attempt.reviewed_by_id is not None

    def test_pending_is_not_a_decision(
        self, client: TestClient, db: Session, teacher_a: dict
    ) -> None:
        attempt = self._attempt(db)
        response = client.post(
            f"/api/attempts/{attempt.id}/proctoring/review",
            json={"decision": "PENDING"},
            headers=teacher_a,
        )
        assert response.status_code == 422

    def test_there_is_no_cheating_decision_to_record(
        self, client: TestClient, db: Session, teacher_a: dict
    ) -> None:
        """Academic misconduct is a process outside a quiz platform."""
        attempt = self._attempt(db)
        response = client.post(
            f"/api/attempts/{attempt.id}/proctoring/review",
            json={"decision": "CHEATING"},
            headers=teacher_a,
        )
        assert response.status_code == 422

    def test_a_review_does_not_change_the_mark(
        self, client: TestClient, db: Session, teacher_a: dict
    ) -> None:
        attempt = self._attempt(db)
        attempt.score = 8
        attempt.max_score = 10
        attempt.percentage = 80
        db.commit()
        client.post(
            f"/api/attempts/{attempt.id}/proctoring/review",
            json={"decision": "REVIEWED_ESCALATED", "note": "Referred to the board."},
            headers=teacher_a,
        )
        db.refresh(attempt)
        assert float(attempt.score) == 8.0
        assert float(attempt.percentage) == 80.0

    def test_another_teacher_cannot_record_a_decision(
        self, client: TestClient, db: Session, teacher_a: dict, teacher_b: dict
    ) -> None:
        attempt = self._attempt(db)
        response = client.post(
            f"/api/attempts/{attempt.id}/proctoring/review",
            json={"decision": "REVIEWED_CONCERN"},
            headers=teacher_b,
        )
        assert response.status_code == 404


class TestRetentionActuallyDeletes:
    """"Kept for 30 days" has to mean the object goes away, not just the row."""

    def test_the_storage_interface_requires_a_delete(self) -> None:
        """Without it the purge job would delete rows and orphan recordings."""
        from app.storage.base import StorageProvider

        assert "delete" in StorageProvider.__abstractmethods__

    @pytest.mark.anyio
    async def test_deleting_an_absent_object_is_a_success(self) -> None:
        """The purge runs on a schedule and retries; a 404 must not block a row
        from ever being cleaned up."""
        from app.storage.stub import StubStorageProvider

        storage = StubStorageProvider()
        await storage.delete(bucket="evidence", path="never/existed.jpg")

    @pytest.mark.anyio
    async def test_a_purge_removes_the_object_and_then_the_row(
        self, db: Session
    ) -> None:
        from app.storage.stub import StubStorageProvider

        storage = StubStorageProvider()
        attempt, _token, _q = full_attempt(db)
        quiz = db.get(Quiz, attempt.quiz_id)
        assert quiz is not None
        quiz.proctoring_mode = ProctoringMode.PROCTORED
        quiz.proctoring_config_json = {"require_camera": True, "store_evidence": True}
        db.commit()

        captured = await evidence_service.capture(
            db,
            attempt,
            policy_for(quiz),
            kind=EvidenceKind.EVENT_SNAPSHOT,
            content=PNG,
            content_type="image/png",
            provider=storage,
        )
        db.commit()
        assert captured.evidence is not None
        key = f"{captured.evidence.bucket}/{captured.evidence.object_path}"
        assert key in storage.objects

        # Retention has closed.
        captured.evidence.retention_expires_at = utcnow() - timedelta(days=1)
        db.commit()

        due = evidence_service.expired_evidence(db)
        assert len(due) == 1
        for row in due:
            await storage.delete(bucket=row.bucket, path=row.object_path)
            db.delete(row)
        db.commit()

        assert key not in storage.objects
        assert db.query(ProctoringEvidence).count() == 0
