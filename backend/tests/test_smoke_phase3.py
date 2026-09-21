"""End-to-end student journey: link -> start -> answer -> submit -> result."""
from __future__ import annotations

from fastapi.testclient import TestClient
from tests.test_ownership import make_family, make_quiz


def publish_quiz(client: TestClient, headers: dict[str, str], questions: int = 2) -> dict:
    quiz = make_quiz(client, headers, question_count=questions, show_score_after_submission=True)
    for _ in range(questions):
        family = make_family(client, headers, quiz["id"])
        body = client.post(f"/api/questions/{family['id']}/generate-variants",
                           headers=headers, json={"count": 3}).json()
        for v in body["variants"][:2]:
            client.post(f"/api/variants/{v['id']}/approve", headers=headers)
    published = client.post(f"/api/quizzes/{quiz['id']}/publish", headers=headers)
    assert published.status_code == 200, published.text
    return published.json()


def test_student_completes_a_quiz_end_to_end(client: TestClient, teacher_a: dict[str, str]) -> None:
    quiz = publish_quiz(client, teacher_a)
    code = quiz["public_code"]

    # 1. Public landing
    public = client.get(f"/api/public/quizzes/{code}")
    assert public.status_code == 200, public.text
    assert public.json()["title"]
    assert "correct_option" not in public.text

    # 2. Start
    start = client.post(f"/api/public/quizzes/{code}/attempts",
                        json={"student_name": "Asha", "student_email": "asha@uni.edu"})
    assert start.status_code == 201, start.text
    body = start.json()
    attempt_id, token = body["attempt_id"], body["attempt_token"]
    assert body["seconds_remaining"] > 0
    auth = {"X-Attempt-Token": token}

    # 3. Paper
    qs = client.get(f"/api/student/attempts/{attempt_id}/questions", headers=auth)
    assert qs.status_code == 200, qs.text
    questions = qs.json()["questions"]
    assert len(questions) == 2
    assert "correct_option" not in qs.text

    # 4. Answer every question
    for question in questions:
        r = client.post(f"/api/student/attempts/{attempt_id}/answers", headers=auth,
                        json={"attempt_question_id": question["attempt_question_id"],
                              "selected_answer": {"displayed_index": 0}})
        assert r.status_code == 200, r.text
        assert "is_correct" not in r.text

    # 5. State reflects progress
    state = client.get(f"/api/student/attempts/{attempt_id}", headers=auth).json()
    assert state["answered_count"] == 2
    assert state["status"] == "IN_PROGRESS"

    # 6. Integrity signal below threshold
    ev = client.post(f"/api/student/attempts/{attempt_id}/integrity-events", headers=auth,
                     json={"event_type": "TAB_HIDDEN"})
    assert ev.status_code == 200, ev.text
    assert ev.json()["auto_submitted"] is False
    assert ev.json()["violation_total"] == 1

    # 7. Submit
    sub = client.post(f"/api/student/attempts/{attempt_id}/submit", headers=auth, json={})
    assert sub.status_code == 200, sub.text
    assert sub.json()["status"] == "SUBMITTED"
    assert sub.json()["score_visible"] is True
    assert sub.json()["max_score"] == 2.0

    # 8. Result readable, idempotent submit
    again = client.post(f"/api/student/attempts/{attempt_id}/submit", headers=auth, json={})
    assert again.json()["submitted_at"] == sub.json()["submitted_at"]
    res = client.get(f"/api/student/attempts/{attempt_id}/result", headers=auth)
    assert res.status_code == 200
    assert "correct_option" not in res.text
