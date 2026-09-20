"""Answer-key non-disclosure.

Two layers are tested:

1. **Structural** — the student-facing schema has no field capable of holding a
   key, so a handler passing the wrong object still cannot leak one.
2. **Behavioural** — no unauthenticated or student-reachable response contains
   an answer-key field anywhere in its JSON.

The teacher-facing schema *does* carry the key, by design: a teacher reviewing
a question must see the answer. What protects it is ownership, tested in
test_ownership.py.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.schemas.question import FORBIDDEN_STUDENT_FIELDS, StudentQuestionOut
from tests.test_ownership import make_family, make_quiz


def collect_keys(payload: Any, found: set[str] | None = None) -> set[str]:
    """Every key appearing anywhere in a JSON structure, at any depth."""
    found = found if found is not None else set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            found.add(key)
            collect_keys(value, found)
    elif isinstance(payload, list):
        for item in payload:
            collect_keys(item, found)
    return found


class TestStudentSchemaShape:
    """The structural guarantee: the serializer has nowhere to put a key."""

    def test_the_student_schema_has_no_answer_key_field(self) -> None:
        leaked = set(StudentQuestionOut.model_fields) & FORBIDDEN_STUDENT_FIELDS
        assert not leaked, f"student schema exposes: {sorted(leaked)}"

    def test_the_student_schema_carries_only_what_a_student_needs(self) -> None:
        assert set(StudentQuestionOut.model_fields) == {
            "attempt_question_id",
            "position",
            "question_type",
            "question_text",
            "options",
            "selected_answer",
        }

    def test_extra_fields_are_dropped_not_passed_through(self) -> None:
        """Even handed the full ORM shape, the schema emits only its own fields."""
        model = StudentQuestionOut.model_validate(
            {
                "attempt_question_id": "11111111-1111-1111-1111-111111111111",
                "position": 0,
                "question_type": "MCQ",
                "question_text": "What is 2 + 2?",
                "options": ["3", "4", "5", "6"],
                "correct_option": 1,
                "numeric_answer_json": {"value": 4},
                "explanation": "Because arithmetic",
                "variant_id": "22222222-2222-2222-2222-222222222222",
                "options_order_json": [2, 0, 3, 1],
            }
        )
        serialized = model.model_dump(mode="json")
        assert not set(serialized) & FORBIDDEN_STUDENT_FIELDS
        assert "correct_option" not in json.dumps(serialized)

    def test_the_forbidden_list_covers_every_key_bearing_column(self) -> None:
        """If a key-bearing column is added, it must be added here too."""
        from app.db.models import QuestionVariant

        key_columns = {
            "correct_option",
            "numeric_answer_json",
            "explanation",
            "content_hash",
            "validation_status",
        }
        assert key_columns <= set(QuestionVariant.__table__.columns.keys())
        assert key_columns <= FORBIDDEN_STUDENT_FIELDS


class TestNoUnauthenticatedDisclosure:
    """No route reachable without a teacher token may return a key."""

    def test_unauthenticated_requests_never_return_question_content(
        self, client: TestClient, teacher_a: dict[str, str]
    ) -> None:
        quiz = make_quiz(client, teacher_a)
        family = make_family(client, teacher_a, quiz["id"])
        body = client.post(
            f"/api/questions/{family['id']}/generate-variants",
            headers=teacher_a,
            json={"count": 2},
        ).json()
        variant_id = body["variants"][0]["id"]

        for method, path in [
            ("get", f"/api/quizzes/{quiz['id']}"),
            ("get", f"/api/quizzes/{quiz['id']}/questions"),
            ("get", f"/api/questions/{family['id']}"),
            ("get", f"/api/questions/{family['id']}/variants"),
            ("get", f"/api/variants/{variant_id}"),
        ]:
            response = getattr(client, method)(path)
            assert response.status_code == 401, f"{path} is reachable without auth"
            assert not collect_keys(response.json()) & FORBIDDEN_STUDENT_FIELDS

    def test_health_endpoints_disclose_nothing(self, client: TestClient) -> None:
        for path in ("/api/health", "/api/health/ready"):
            keys = collect_keys(client.get(path).json())
            assert not keys & FORBIDDEN_STUDENT_FIELDS

    def test_error_responses_do_not_echo_answer_keys(
        self, client: TestClient, teacher_a: dict[str, str], teacher_b: dict[str, str]
    ) -> None:
        """A 404 for someone else's variant must not describe it."""
        quiz = make_quiz(client, teacher_b)
        family = make_family(client, teacher_b, quiz["id"])
        variant_id = client.post(
            f"/api/questions/{family['id']}/generate-variants",
            headers=teacher_b,
            json={"count": 1},
        ).json()["variants"][0]["id"]

        response = client.get(f"/api/variants/{variant_id}", headers=teacher_a)
        assert response.status_code == 404
        assert not collect_keys(response.json()) & FORBIDDEN_STUDENT_FIELDS


class TestTeacherDisclosureIsIntentional:
    """The owner sees the key. That is the point of a review screen."""

    def test_the_owner_can_see_the_correct_option(
        self, client: TestClient, teacher_a: dict[str, str]
    ) -> None:
        quiz = make_quiz(client, teacher_a)
        family = make_family(client, teacher_a, quiz["id"])
        variant = client.post(
            f"/api/questions/{family['id']}/generate-variants",
            headers=teacher_a,
            json={"count": 1},
        ).json()["variants"][0]
        assert variant["correct_option"] is not None
        assert variant["explanation"]

    def test_only_the_owner_sees_it(
        self, client: TestClient, teacher_a: dict[str, str], teacher_b: dict[str, str]
    ) -> None:
        quiz = make_quiz(client, teacher_b)
        family = make_family(client, teacher_b, quiz["id"])
        variant_id = client.post(
            f"/api/questions/{family['id']}/generate-variants",
            headers=teacher_b,
            json={"count": 1},
        ).json()["variants"][0]["id"]
        assert client.get(f"/api/variants/{variant_id}", headers=teacher_a).status_code == 404


class TestPromptAndConfigConfidentiality:
    def test_prompts_are_never_returned_by_any_route(
        self, client: TestClient, teacher_a: dict[str, str]
    ) -> None:
        """A prompt version is provenance; the prompt text itself is internal."""
        quiz = make_quiz(client, teacher_a)
        family = make_family(client, teacher_a, quiz["id"])
        body = client.post(
            f"/api/questions/{family['id']}/generate-variants",
            headers=teacher_a,
            json={"count": 1},
        ).json()
        serialized = json.dumps(body)
        assert "You write multiple-choice exam questions" not in serialized
        assert "Rules you must follow" not in serialized
        assert body["variants"][0]["generation_prompt_version"]

    def test_generation_config_is_not_on_the_student_schema(self) -> None:
        assert "generation_config_json" not in StudentQuestionOut.model_fields
        assert "generation_config_json" in FORBIDDEN_STUDENT_FIELDS

    @pytest.mark.parametrize(
        "secret_marker", ["GEMINI_API_KEY", "SUPABASE_SERVICE_ROLE_KEY", "x-goog-api-key"]
    )
    def test_no_response_contains_a_credential_marker(
        self, client: TestClient, teacher_a: dict[str, str], secret_marker: str
    ) -> None:
        quiz = make_quiz(client, teacher_a)
        body = client.get(f"/api/quizzes/{quiz['id']}", headers=teacher_a).text
        assert secret_marker not in body
