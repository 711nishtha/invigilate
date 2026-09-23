"""Adversarial tests for results, export, email and storage.

Two questions each test asks:

* can teacher A reach anything belonging to teacher B?
* can a failure of Gemini, Resend or Storage corrupt or roll back a result?

Both answers must be no.
"""

from __future__ import annotations

import io
import uuid

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Attempt, DeliveryStatus, ResultDelivery
from app.email.base import EmailRateLimited, EmailRejected, EmailTimeout
from app.email.factory import set_email_provider
from app.email.stub import StubEmailProvider
from app.storage.base import StorageRejected, StorageUnavailable
from app.storage.factory import set_storage_provider
from app.storage.stub import StubStorageProvider
from tests.test_student_security import auth, publish, start


def complete_attempt(
    client: TestClient,
    code: str,
    *,
    email: str = "asha@uni.edu",
    name: str = "Asha",
    submit: bool = True,
) -> dict:
    """A student sits the exam end to end."""
    session = start(client, code, email=email, name=name)
    headers = auth(session["attempt_token"])
    questions = client.get(
        f"/api/student/attempts/{session['attempt_id']}/questions", headers=headers
    ).json()["questions"]
    for question in questions:
        client.post(
            f"/api/student/attempts/{session['attempt_id']}/answers",
            headers=headers,
            json={
                "attempt_question_id": question["attempt_question_id"],
                "selected_answer": {"displayed_index": 0},
            },
        )
    if submit:
        client.post(
            f"/api/student/attempts/{session['attempt_id']}/submit", headers=headers, json={}
        )
    return session


@pytest.fixture
def quiz_with_results(client: TestClient, teacher_a: dict[str, str]) -> dict:
    quiz = publish(client, teacher_a, questions=2, variants=1, show_score_after_submission=True)
    complete_attempt(client, quiz["public_code"], email="asha@uni.edu", name="Asha")
    return quiz


@pytest.fixture
def b_quiz_with_results(client: TestClient, teacher_b: dict[str, str]) -> dict:
    quiz = publish(client, teacher_b, questions=2, variants=1)
    complete_attempt(client, quiz["public_code"], email="bee@uni.edu", name="Bee")
    return quiz


# --- Teacher ownership -----------------------------------------------------


class TestCrossTeacherResults:
    def test_a_cannot_read_bs_results(
        self, client: TestClient, teacher_a: dict[str, str], b_quiz_with_results: dict
    ) -> None:
        response = client.get(
            f"/api/quizzes/{b_quiz_with_results['id']}/results", headers=teacher_a
        )
        assert response.status_code == 404

    def test_a_cannot_export_bs_quiz(
        self, client: TestClient, teacher_a: dict[str, str], b_quiz_with_results: dict
    ) -> None:
        response = client.post(
            f"/api/quizzes/{b_quiz_with_results['id']}/export", headers=teacher_a
        )
        assert response.status_code == 404

    def test_a_cannot_email_bs_results(
        self, client: TestClient, teacher_a: dict[str, str], b_quiz_with_results: dict
    ) -> None:
        response = client.post(
            f"/api/quizzes/{b_quiz_with_results['id']}/email-results",
            headers=teacher_a,
            json={},
        )
        assert response.status_code == 404

    def test_a_cannot_list_bs_deliveries(
        self, client: TestClient, teacher_a: dict[str, str], b_quiz_with_results: dict
    ) -> None:
        response = client.get(
            f"/api/quizzes/{b_quiz_with_results['id']}/deliveries", headers=teacher_a
        )
        assert response.status_code == 404

    def test_a_cannot_inspect_bs_attempt(
        self, client: TestClient, teacher_a: dict[str, str], teacher_b: dict[str, str],
        b_quiz_with_results: dict
    ) -> None:
        b_results = client.get(
            f"/api/quizzes/{b_quiz_with_results['id']}/results", headers=teacher_b
        ).json()
        attempt_id = b_results["attempts"][0]["attempt_id"]

        response = client.get(f"/api/attempts/{attempt_id}/detail", headers=teacher_a)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "ATTEMPT_NOT_FOUND"

    def test_a_cannot_download_bs_retained_export(
        self, client: TestClient, teacher_a: dict[str, str], teacher_b: dict[str, str],
        b_quiz_with_results: dict, db: Session
    ) -> None:
        from app.core.config import settings

        settings.retain_exports = True
        try:
            client.post(
                f"/api/quizzes/{b_quiz_with_results['id']}/email-results",
                headers=teacher_b,
                json={},
            )
            delivery = db.scalar(select(ResultDelivery))
            assert delivery is not None
            response = client.get(
                f"/api/deliveries/{delivery.id}/export", headers=teacher_a
            )
            assert response.status_code == 404
        finally:
            settings.retain_exports = False

    def test_bs_data_is_unchanged_after_every_attack(
        self, client: TestClient, teacher_a: dict[str, str], teacher_b: dict[str, str],
        b_quiz_with_results: dict
    ) -> None:
        before = client.get(
            f"/api/quizzes/{b_quiz_with_results['id']}/results", headers=teacher_b
        ).json()
        for call in (
            lambda: client.post(
                f"/api/quizzes/{b_quiz_with_results['id']}/export", headers=teacher_a),
            lambda: client.post(
                f"/api/quizzes/{b_quiz_with_results['id']}/email-results",
                headers=teacher_a, json={}),
        ):
            assert call().status_code == 404
        after = client.get(
            f"/api/quizzes/{b_quiz_with_results['id']}/results", headers=teacher_b
        ).json()
        assert after == before

    @pytest.mark.parametrize("bad_id", ["not-a-uuid", "1 OR 1=1", "../../x", "0"])
    def test_malformed_ids_are_404_not_500(
        self, client: TestClient, teacher_a: dict[str, str], bad_id: str
    ) -> None:
        for path in (
            f"/api/quizzes/{bad_id}/results",
            f"/api/attempts/{bad_id}/detail",
            f"/api/quizzes/{bad_id}/deliveries",
            f"/api/deliveries/{bad_id}/export",
        ):
            assert client.get(path, headers=teacher_a).status_code == 404, path

    def test_an_unknown_and_a_forbidden_quiz_are_indistinguishable(
        self, client: TestClient, teacher_a: dict[str, str], b_quiz_with_results: dict
    ) -> None:
        forbidden = client.get(
            f"/api/quizzes/{b_quiz_with_results['id']}/results", headers=teacher_a
        )
        missing = client.get(f"/api/quizzes/{uuid.uuid4()}/results", headers=teacher_a)
        assert forbidden.status_code == missing.status_code == 404
        assert forbidden.json() == missing.json()

    def test_results_require_authentication(
        self, client: TestClient, quiz_with_results: dict
    ) -> None:
        for method, path in (
            ("get", f"/api/quizzes/{quiz_with_results['id']}/results"),
            ("post", f"/api/quizzes/{quiz_with_results['id']}/export"),
            ("post", f"/api/quizzes/{quiz_with_results['id']}/email-results"),
            ("get", f"/api/quizzes/{quiz_with_results['id']}/deliveries"),
        ):
            kwargs = {"json": {}} if method == "post" else {}
            assert getattr(client, method)(path, **kwargs).status_code == 401, path


class TestNoCrossQuizLeakage:
    def test_results_contain_only_this_quizs_students(
        self, client: TestClient, teacher_a: dict[str, str]
    ) -> None:
        first = publish(client, teacher_a, questions=1, variants=1)
        second = publish(client, teacher_a, questions=1, variants=1)
        complete_attempt(client, first["public_code"], email="one@uni.edu", name="StudentOne")
        complete_attempt(client, second["public_code"], email="two@uni.edu", name="StudentTwo")

        body = client.get(f"/api/quizzes/{first['id']}/results", headers=teacher_a).text
        assert "StudentOne" in body
        assert "StudentTwo" not in body, "a quiz must not show another quiz's students"

    def test_the_export_contains_only_this_quizs_students(
        self, client: TestClient, teacher_a: dict[str, str]
    ) -> None:
        first = publish(client, teacher_a, questions=1, variants=1)
        second = publish(client, teacher_a, questions=1, variants=1)
        complete_attempt(client, first["public_code"], email="one@uni.edu", name="StudentOne")
        complete_attempt(client, second["public_code"], email="two@uni.edu", name="StudentTwo")

        export = client.post(f"/api/quizzes/{first['id']}/export", headers=teacher_a)
        text = _workbook_text(export.content)
        assert "StudentOne" in text
        assert "StudentTwo" not in text


def _workbook_text(payload: bytes) -> str:
    """Every cell of every sheet as one string, for leakage assertions."""
    workbook = load_workbook(io.BytesIO(payload))
    chunks: list[str] = []
    for sheet in workbook.worksheets:
        chunks.append(sheet.title)
        for row in sheet.iter_rows(values_only=True):
            chunks.extend(str(cell) for cell in row if cell is not None)
    return "\n".join(chunks)


# --- Export integrity ------------------------------------------------------


class TestExportIntegrity:
    def test_the_export_carries_no_tokens_or_secrets(
        self, client: TestClient, teacher_a: dict[str, str], quiz_with_results: dict, db: Session
    ) -> None:
        from app.services.export_service import FORBIDDEN_EXPORT_FIELDS

        export = client.post(f"/api/quizzes/{quiz_with_results['id']}/export", headers=teacher_a)
        text = _workbook_text(export.content).lower()
        for field in FORBIDDEN_EXPORT_FIELDS:
            assert field not in text, f"export leaks {field}"

        # And no actual token value from the database.
        attempt = db.scalar(select(Attempt))
        assert attempt is not None
        assert attempt.attempt_token_hash not in _workbook_text(export.content)
        assert str(attempt.seed) not in _workbook_text(export.content)

    def test_export_marks_match_the_database_not_the_client(
        self, client: TestClient, teacher_a: dict[str, str], quiz_with_results: dict, db: Session
    ) -> None:
        """A tampered client payload cannot change what the workbook says."""
        attempt = db.scalar(select(Attempt))
        assert attempt is not None

        export = client.post(f"/api/quizzes/{quiz_with_results['id']}/export", headers=teacher_a)
        workbook = load_workbook(io.BytesIO(export.content))
        summary = {
            row[0]: row[1]
            for row in workbook["Summary"].iter_rows(values_only=True)
            if row and row[0]
        }
        assert summary["Total started"] == 1
        assert summary["Finalized"] == 1
        # The percentage the database stored, not anything a browser sent.
        assert summary["Average (%)"] == float(attempt.percentage)

    def test_an_empty_quiz_exports_cleanly(
        self, client: TestClient, teacher_a: dict[str, str]
    ) -> None:
        """Zero attempts must be an honest empty report, not a crash or a fake zero."""
        quiz = publish(client, teacher_a, questions=1, variants=1)
        export = client.post(f"/api/quizzes/{quiz['id']}/export", headers=teacher_a)
        assert export.status_code == 200

        workbook = load_workbook(io.BytesIO(export.content))
        assert workbook.sheetnames == ["Summary", "Answers", "Integrity", "Question statistics"]
        assert workbook["Answers"].max_row == 1, "headers only"
        summary = {
            row[0]: row[1]
            for row in workbook["Summary"].iter_rows(values_only=True)
            if row and row[0]
        }
        assert summary["Total started"] == 0
        assert summary["Average (%)"] == "n/a", "no attempts must not read as a zero average"

    def test_an_in_progress_attempt_is_not_graded_in_the_export(
        self, client: TestClient, teacher_a: dict[str, str]
    ) -> None:
        quiz = publish(client, teacher_a, questions=1, variants=1)
        complete_attempt(client, quiz["public_code"], email="mid@uni.edu", submit=False)

        export = client.post(f"/api/quizzes/{quiz['id']}/export", headers=teacher_a)
        text = _workbook_text(export.content)
        assert "not graded" in text, "an unfinalized answer must not claim a verdict"

    def test_unanswered_questions_are_labelled(
        self, client: TestClient, teacher_a: dict[str, str]
    ) -> None:
        quiz = publish(client, teacher_a, questions=2, variants=1)
        session = start(client, quiz["public_code"], email="partial@uni.edu")
        headers = auth(session["attempt_token"])
        questions = client.get(
            f"/api/student/attempts/{session['attempt_id']}/questions", headers=headers
        ).json()["questions"]
        client.post(
            f"/api/student/attempts/{session['attempt_id']}/answers",
            headers=headers,
            json={
                "attempt_question_id": questions[0]["attempt_question_id"],
                "selected_answer": {"displayed_index": 0},
            },
        )
        client.post(
            f"/api/student/attempts/{session['attempt_id']}/submit", headers=headers, json={}
        )

        text = _workbook_text(
            client.post(f"/api/quizzes/{quiz['id']}/export", headers=teacher_a).content
        )
        assert "unanswered" in text

    def test_an_auto_submitted_attempt_appears_with_its_events(
        self, client: TestClient, teacher_a: dict[str, str]
    ) -> None:
        quiz = publish(client, teacher_a, questions=1, variants=1, max_integrity_violations=0)
        session = start(client, quiz["public_code"], email="flag@uni.edu")
        client.post(
            f"/api/student/attempts/{session['attempt_id']}/integrity-events",
            headers=auth(session["attempt_token"]),
            json={"event_type": "TAB_HIDDEN"},
        )

        text = _workbook_text(
            client.post(f"/api/quizzes/{quiz['id']}/export", headers=teacher_a).content
        )
        assert "AUTO_SUBMITTED" in text
        assert "TAB_HIDDEN" in text
        assert "AUTO_SUBMISSION_TRIGGERED" in text

    def test_unicode_names_and_questions_survive(
        self, client: TestClient, teacher_a: dict[str, str]
    ) -> None:
        quiz = publish(client, teacher_a, questions=1, variants=1)
        complete_attempt(
            client, quiz["public_code"], email="uni@uni.edu", name="Ana Ruiz-Muñoz 王小明"
        )
        text = _workbook_text(
            client.post(f"/api/quizzes/{quiz['id']}/export", headers=teacher_a).content
        )
        assert "Ana Ruiz-Muñoz 王小明" in text

    def test_many_attempts_export_completely(
        self, client: TestClient, teacher_a: dict[str, str]
    ) -> None:
        quiz = publish(client, teacher_a, questions=2, variants=2)
        for index in range(12):
            complete_attempt(
                client, quiz["public_code"], email=f"s{index}@uni.edu", name=f"Student{index}"
            )

        export = client.post(f"/api/quizzes/{quiz['id']}/export", headers=teacher_a)
        workbook = load_workbook(io.BytesIO(export.content))
        assert workbook["Answers"].max_row == 1 + 12 * 2
        text = _workbook_text(export.content)
        for index in range(12):
            assert f"Student{index}" in text

    def test_question_statistics_count_correctly(
        self, client: TestClient, teacher_a: dict[str, str]
    ) -> None:
        quiz = publish(client, teacher_a, questions=1, variants=1)
        for index in range(3):
            complete_attempt(client, quiz["public_code"], email=f"q{index}@uni.edu")

        workbook = load_workbook(
            io.BytesIO(
                client.post(f"/api/quizzes/{quiz['id']}/export", headers=teacher_a).content
            )
        )
        rows = list(workbook["Question statistics"].iter_rows(values_only=True))
        header, data = rows[0], rows[1:]
        presented = header.index("Presented")
        answered = header.index("Answered")
        assert sum(row[presented] for row in data) == 3
        assert sum(row[answered] for row in data) == 3


# --- Email failure never costs a result ------------------------------------


class TestEmailFailureIsolation:
    @pytest.mark.parametrize(
        ("error", "code"),
        [
            (EmailTimeout("slow"), "EmailTimeout"),
            (EmailRateLimited("429", retry_after=30), "EmailRateLimited"),
            (EmailRejected("bad key"), "EmailRejected"),
        ],
    )
    def test_a_send_failure_is_reported_not_raised(
        self, client: TestClient, teacher_a: dict[str, str], quiz_with_results: dict,
        error: Exception, code: str
    ) -> None:
        set_email_provider(StubEmailProvider(fail_with=error))  # type: ignore[arg-type]
        response = client.post(
            f"/api/quizzes/{quiz_with_results['id']}/email-results", headers=teacher_a, json={}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["sent"] is False
        assert body["error_code"] == code
        assert body["delivery"]["status"] == "FAILED"

    def test_a_send_failure_leaves_every_result_untouched(
        self, client: TestClient, teacher_a: dict[str, str], quiz_with_results: dict
    ) -> None:
        """The exam is academic record; Resend is an errand."""
        before = client.get(
            f"/api/quizzes/{quiz_with_results['id']}/results", headers=teacher_a
        ).json()

        set_email_provider(StubEmailProvider(fail_with=EmailTimeout("down")))
        client.post(
            f"/api/quizzes/{quiz_with_results['id']}/email-results", headers=teacher_a, json={}
        )

        after = client.get(
            f"/api/quizzes/{quiz_with_results['id']}/results", headers=teacher_a
        ).json()
        assert after["stats"] == before["stats"]
        assert after["attempts"] == before["attempts"]

    def test_a_failed_delivery_can_be_retried_successfully(
        self, client: TestClient, teacher_a: dict[str, str], quiz_with_results: dict
    ) -> None:
        set_email_provider(StubEmailProvider(fail_with=EmailTimeout("down")))
        first = client.post(
            f"/api/quizzes/{quiz_with_results['id']}/email-results", headers=teacher_a, json={}
        ).json()
        assert first["sent"] is False

        working = StubEmailProvider()
        set_email_provider(working)
        second = client.post(
            f"/api/quizzes/{quiz_with_results['id']}/email-results", headers=teacher_a, json={}
        ).json()
        assert second["sent"] is True
        assert len(working.sent) == 1

        history = client.get(
            f"/api/quizzes/{quiz_with_results['id']}/deliveries", headers=teacher_a
        ).json()
        statuses = sorted(row["status"] for row in history)
        assert statuses == ["FAILED", "SENT"], "both attempts are recorded"

    def test_retrying_does_not_duplicate_results(
        self, client: TestClient, teacher_a: dict[str, str], quiz_with_results: dict, db: Session
    ) -> None:
        for _ in range(3):
            client.post(
                f"/api/quizzes/{quiz_with_results['id']}/email-results",
                headers=teacher_a,
                json={},
            )
        assert len(db.scalars(select(Attempt)).all()) == 1

    def test_the_email_goes_to_the_quiz_owner_only(
        self, client: TestClient, teacher_a: dict[str, str], quiz_with_results: dict,
        stub_external_providers: dict
    ) -> None:
        client.post(
            f"/api/quizzes/{quiz_with_results['id']}/email-results", headers=teacher_a, json={}
        )
        sent = stub_external_providers["email"].sent
        assert len(sent) == 1
        assert sent[0].to == "teacher.a@example.edu"

    def test_a_client_cannot_choose_the_recipient(
        self, client: TestClient, teacher_a: dict[str, str], quiz_with_results: dict
    ) -> None:
        """Otherwise the endpoint is an open relay for student marks."""
        response = client.post(
            f"/api/quizzes/{quiz_with_results['id']}/email-results",
            headers=teacher_a,
            json={"to": "attacker@evil.example", "recipient": "attacker@evil.example"},
        )
        assert response.status_code == 422

    def test_the_email_carries_no_secrets(
        self, client: TestClient, teacher_a: dict[str, str], quiz_with_results: dict,
        stub_external_providers: dict
    ) -> None:
        client.post(
            f"/api/quizzes/{quiz_with_results['id']}/email-results", headers=teacher_a, json={}
        )
        message = stub_external_providers["email"].sent[0]
        blob = f"{message.subject}\n{message.html}\n{message.text}"
        for secret in ("RESEND_API_KEY", "SERVICE_ROLE", "attempt_token", "Bearer "):
            assert secret not in blob

    def test_the_email_links_to_the_dashboard_not_a_public_file(
        self, client: TestClient, teacher_a: dict[str, str], quiz_with_results: dict,
        stub_external_providers: dict
    ) -> None:
        client.post(
            f"/api/quizzes/{quiz_with_results['id']}/email-results", headers=teacher_a, json={}
        )
        message = stub_external_providers["email"].sent[0]
        assert "/teacher/quizzes/" in message.html
        assert "storage" not in message.html.lower()
        # The workbook rides as an attachment rather than a link.
        assert len(message.attachments) == 1
        assert message.attachments[0].filename.endswith(".xlsx")


class TestStorageFailureIsolation:
    def test_an_upload_failure_still_sends_the_email(
        self, client: TestClient, teacher_a: dict[str, str], quiz_with_results: dict
    ) -> None:
        """Storage is a convenience; the workbook is regenerable."""
        from app.core.config import settings

        settings.retain_exports = True
        set_storage_provider(StubStorageProvider(fail_with=StorageUnavailable("bucket down")))
        try:
            response = client.post(
                f"/api/quizzes/{quiz_with_results['id']}/email-results",
                headers=teacher_a,
                json={},
            )
            assert response.status_code == 200
            assert response.json()["sent"] is True
            assert response.json()["delivery"]["retained"] is False
        finally:
            settings.retain_exports = False

    def test_an_upload_failure_leaves_results_untouched(
        self, client: TestClient, teacher_a: dict[str, str], quiz_with_results: dict
    ) -> None:
        from app.core.config import settings

        before = client.get(
            f"/api/quizzes/{quiz_with_results['id']}/results", headers=teacher_a
        ).json()
        settings.retain_exports = True
        set_storage_provider(StubStorageProvider(fail_with=StorageRejected("no bucket")))
        try:
            client.post(
                f"/api/quizzes/{quiz_with_results['id']}/email-results",
                headers=teacher_a,
                json={},
            )
        finally:
            settings.retain_exports = False

        after = client.get(
            f"/api/quizzes/{quiz_with_results['id']}/results", headers=teacher_a
        ).json()
        assert after == before

    def test_a_retained_export_is_downloadable_by_its_owner(
        self, client: TestClient, teacher_a: dict[str, str], quiz_with_results: dict, db: Session
    ) -> None:
        from app.core.config import settings

        settings.retain_exports = True
        try:
            client.post(
                f"/api/quizzes/{quiz_with_results['id']}/email-results",
                headers=teacher_a,
                json={},
            )
            delivery = db.scalar(select(ResultDelivery))
            assert delivery is not None and delivery.export_object_path
            response = client.get(f"/api/deliveries/{delivery.id}/export", headers=teacher_a)
            assert response.status_code == 200
            assert response.json()["url"].startswith("https://")
        finally:
            settings.retain_exports = False

    def test_a_delivery_without_a_retained_export_is_a_404(
        self, client: TestClient, teacher_a: dict[str, str], quiz_with_results: dict, db: Session
    ) -> None:
        client.post(
            f"/api/quizzes/{quiz_with_results['id']}/email-results", headers=teacher_a, json={}
        )
        delivery = db.scalar(select(ResultDelivery))
        assert delivery is not None and delivery.export_object_path is None
        response = client.get(f"/api/deliveries/{delivery.id}/export", headers=teacher_a)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "EXPORT_NOT_RETAINED"

    def test_the_export_path_is_namespaced_by_quiz(
        self, client: TestClient, teacher_a: dict[str, str], quiz_with_results: dict, db: Session
    ) -> None:
        """So one quiz's export can never overwrite or expose another's."""
        from app.core.config import settings

        settings.retain_exports = True
        try:
            client.post(
                f"/api/quizzes/{quiz_with_results['id']}/email-results",
                headers=teacher_a,
                json={},
            )
            delivery = db.scalar(select(ResultDelivery))
            assert delivery is not None
            assert delivery.export_object_path.startswith(
                f"quizzes/{quiz_with_results['id']}/"
            )
        finally:
            settings.retain_exports = False


# --- Delivery record -------------------------------------------------------


class TestDeliveryRecord:
    def test_a_delivery_never_writes_to_attempt_tables(
        self, client: TestClient, teacher_a: dict[str, str], quiz_with_results: dict, db: Session
    ) -> None:
        from app.db.models import Answer, IntegrityEvent

        before = (
            len(db.scalars(select(Attempt)).all()),
            len(db.scalars(select(Answer)).all()),
            len(db.scalars(select(IntegrityEvent)).all()),
        )
        set_email_provider(StubEmailProvider(fail_with=EmailRejected("nope")))
        client.post(
            f"/api/quizzes/{quiz_with_results['id']}/email-results", headers=teacher_a, json={}
        )
        after = (
            len(db.scalars(select(Attempt)).all()),
            len(db.scalars(select(Answer)).all()),
            len(db.scalars(select(IntegrityEvent)).all()),
        )
        assert after == before

    def test_the_failure_detail_is_recorded_without_secrets(
        self, client: TestClient, teacher_a: dict[str, str], quiz_with_results: dict, db: Session
    ) -> None:
        set_email_provider(
            StubEmailProvider(fail_with=EmailRejected("bad key", detail="API key invalid"))
        )
        client.post(
            f"/api/quizzes/{quiz_with_results['id']}/email-results", headers=teacher_a, json={}
        )
        delivery = db.scalar(select(ResultDelivery))
        assert delivery is not None
        assert delivery.status is DeliveryStatus.FAILED
        assert delivery.error_code == "EmailRejected"
        assert delivery.failed_at is not None
        assert "Bearer" not in (delivery.error_detail or "")

    def test_the_delivery_records_how_many_attempts_it_covered(
        self, client: TestClient, teacher_a: dict[str, str], quiz_with_results: dict
    ) -> None:
        response = client.post(
            f"/api/quizzes/{quiz_with_results['id']}/email-results", headers=teacher_a, json={}
        ).json()
        assert response["delivery"]["attempt_count"] == 1
