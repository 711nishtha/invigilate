"""End-to-end teacher workflow: create -> question -> generate -> approve -> publish."""
from __future__ import annotations

from fastapi.testclient import TestClient


def test_full_teacher_workflow(client: TestClient, teacher_a: dict[str, str]) -> None:
    quiz = client.post("/api/quizzes", headers=teacher_a, json={
        "title": "Data Structures Midterm", "duration_seconds": 1200, "question_count": 1,
    }).json()
    assert quiz["status"] == "DRAFT", quiz
    quiz_id = quiz["id"]
    assert quiz["readiness"]["publishable"] is False

    family = client.post(f"/api/quizzes/{quiz_id}/questions", headers=teacher_a, json={
        "original_question": "What is the time complexity of binary search?",
        "question_type": "MCQ", "topic": "Algorithms", "difficulty": "medium",
    }).json()
    assert "id" in family, family
    family_id = family["id"]

    gen = client.post(f"/api/questions/{family_id}/generate-variants",
                      headers=teacher_a, json={"count": 3})
    assert gen.status_code == 200, gen.text
    body = gen.json()
    assert body["accepted"] == 3, body
    assert all(v["validation_status"] == "VALIDATED" for v in body["variants"])

    variant_id = body["variants"][0]["id"]
    approved = client.post(f"/api/variants/{variant_id}/approve", headers=teacher_a)
    assert approved.status_code == 200, approved.text
    assert approved.json()["validation_status"] == "APPROVED"

    published = client.post(f"/api/quizzes/{quiz_id}/publish", headers=teacher_a)
    assert published.status_code == 200, published.text
    detail = published.json()
    assert detail["status"] == "PUBLISHED"
    assert detail["public_code"] and len(detail["public_code"]) == 6
    assert detail["readiness"]["publishable"] is True
