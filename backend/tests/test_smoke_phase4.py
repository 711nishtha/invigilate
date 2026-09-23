"""End-to-end Phase 4: exam completed -> results -> export -> email."""

from __future__ import annotations

import io

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from tests.test_student_security import auth, publish, start


def test_teacher_sees_results_exports_and_emails_them(
    client: TestClient, teacher_a: dict[str, str]
) -> None:
    quiz = publish(client, teacher_a, questions=2, variants=1, show_score_after_submission=True)

    # A student completes the exam.
    session = start(client, quiz["public_code"], email="asha@uni.edu", name="Asha Rao")
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
    client.post(
        f"/api/student/attempts/{session['attempt_id']}/integrity-events",
        headers=headers,
        json={"event_type": "TAB_HIDDEN"},
    )
    client.post(f"/api/student/attempts/{session['attempt_id']}/submit", headers=headers, json={})

    # Teacher results dashboard.
    results = client.get(f"/api/quizzes/{quiz['id']}/results", headers=teacher_a)
    assert results.status_code == 200, results.text
    body = results.json()
    assert body["stats"]["attempt_count"] == 1
    assert body["stats"]["submitted_count"] == 1
    assert body["stats"]["finalized_count"] == 1
    assert body["stats"]["average_percentage"] is not None
    assert body["attempts"][0]["student_name"] == "Asha Rao"
    assert body["attempts"][0]["violation_count"] == 1

    # Attempt detail, including the key the teacher is entitled to see.
    attempt_id = body["attempts"][0]["attempt_id"]
    detail = client.get(f"/api/attempts/{attempt_id}/detail", headers=teacher_a)
    assert detail.status_code == 200, detail.text
    detail_body = detail.json()
    assert len(detail_body["questions"]) == 2
    assert detail_body["questions"][0]["correct_answer"]
    assert detail_body["integrity_events"][0]["event_type"] == "TAB_HIDDEN"

    # Export: a real workbook with four sheets.
    export = client.post(f"/api/quizzes/{quiz['id']}/export", headers=teacher_a)
    assert export.status_code == 200, export.text
    assert export.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument"
    )
    workbook = load_workbook(io.BytesIO(export.content))
    assert workbook.sheetnames == ["Summary", "Answers", "Integrity", "Question statistics"]
    assert workbook["Answers"].max_row == 3  # header + two questions
    assert workbook["Integrity"].max_row == 2  # header + one event

    # Email, through the stub provider.
    email = client.post(f"/api/quizzes/{quiz['id']}/email-results", headers=teacher_a, json={})
    assert email.status_code == 200, email.text
    assert email.json()["sent"] is True
    assert email.json()["delivery"]["status"] == "SENT"
    assert email.json()["delivery"]["attempt_count"] == 1

    # Delivery history records it.
    deliveries = client.get(f"/api/quizzes/{quiz['id']}/deliveries", headers=teacher_a)
    assert deliveries.status_code == 200
    assert len(deliveries.json()) == 1
    assert deliveries.json()[0]["status"] == "SENT"
