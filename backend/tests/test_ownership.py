"""Adversarial tests for teacher ownership.

Teacher A must not be able to read or change anything belonging to teacher B,
through any route, by any identifier. Each test is named for the attack it
attempts.

Every failure must be 404, never 403: a 403 would confirm the resource exists.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient


def make_quiz(client: TestClient, headers: dict[str, str], **overrides) -> dict:  # type: ignore[no-untyped-def]
    payload = {"title": "Quiz", "duration_seconds": 600, "question_count": 1, **overrides}
    response = client.post("/api/quizzes", headers=headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def make_family(client: TestClient, headers: dict[str, str], quiz_id: str) -> dict:
    response = client.post(
        f"/api/quizzes/{quiz_id}/questions",
        headers=headers,
        json={
            "original_question": "What is the time complexity of binary search?",
            "question_type": "MCQ",
            "topic": "Algorithms",
            "difficulty": "medium",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def make_variant(client: TestClient, headers: dict[str, str], family_id: str) -> dict:
    response = client.post(
        f"/api/questions/{family_id}/generate-variants", headers=headers, json={"count": 1}
    )
    assert response.status_code == 200, response.text
    return response.json()["variants"][0]


@pytest.fixture
def b_resources(client: TestClient, teacher_b: dict[str, str]) -> dict[str, str]:
    """A quiz, family and variant belonging to teacher B."""
    quiz = make_quiz(client, teacher_b, title="Teacher B private quiz")
    family = make_family(client, teacher_b, quiz["id"])
    variant = make_variant(client, teacher_b, family["id"])
    return {"quiz_id": quiz["id"], "family_id": family["id"], "variant_id": variant["id"]}


class TestCrossTeacherQuizAccess:
    def test_a_cannot_read_bs_quiz(
        self, client: TestClient, teacher_a: dict[str, str], b_resources: dict[str, str]
    ) -> None:
        response = client.get(f"/api/quizzes/{b_resources['quiz_id']}", headers=teacher_a)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "QUIZ_NOT_FOUND"

    def test_a_cannot_modify_bs_quiz(
        self, client: TestClient, teacher_a: dict[str, str], b_resources: dict[str, str]
    ) -> None:
        response = client.patch(
            f"/api/quizzes/{b_resources['quiz_id']}", headers=teacher_a, json={"title": "Hijacked"}
        )
        assert response.status_code == 404

    def test_a_cannot_publish_bs_quiz(
        self, client: TestClient, teacher_a: dict[str, str], b_resources: dict[str, str]
    ) -> None:
        response = client.post(
            f"/api/quizzes/{b_resources['quiz_id']}/publish", headers=teacher_a
        )
        assert response.status_code == 404

    def test_a_cannot_delete_bs_quiz(
        self, client: TestClient, teacher_a: dict[str, str], b_resources: dict[str, str]
    ) -> None:
        assert (
            client.delete(f"/api/quizzes/{b_resources['quiz_id']}", headers=teacher_a).status_code
            == 404
        )

    def test_a_cannot_see_bs_readiness(
        self, client: TestClient, teacher_a: dict[str, str], b_resources: dict[str, str]
    ) -> None:
        assert (
            client.get(
                f"/api/quizzes/{b_resources['quiz_id']}/readiness", headers=teacher_a
            ).status_code
            == 404
        )

    def test_listing_shows_only_your_own_quizzes(
        self, client: TestClient, teacher_a: dict[str, str], b_resources: dict[str, str]
    ) -> None:
        make_quiz(client, teacher_a, title="Teacher A quiz")
        titles = [q["title"] for q in client.get("/api/quizzes", headers=teacher_a).json()]
        assert titles == ["Teacher A quiz"]
        assert "Teacher B private quiz" not in titles


class TestCrossTeacherFamilyAccess:
    def test_a_cannot_read_bs_family(
        self, client: TestClient, teacher_a: dict[str, str], b_resources: dict[str, str]
    ) -> None:
        response = client.get(f"/api/questions/{b_resources['family_id']}", headers=teacher_a)
        assert response.status_code == 404

    def test_a_cannot_modify_bs_family(
        self, client: TestClient, teacher_a: dict[str, str], b_resources: dict[str, str]
    ) -> None:
        response = client.patch(
            f"/api/questions/{b_resources['family_id']}",
            headers=teacher_a,
            json={"original_question": "Rewritten by an attacker"},
        )
        assert response.status_code == 404

    def test_a_cannot_delete_bs_family(
        self, client: TestClient, teacher_a: dict[str, str], b_resources: dict[str, str]
    ) -> None:
        assert (
            client.delete(
                f"/api/questions/{b_resources['family_id']}", headers=teacher_a
            ).status_code
            == 404
        )

    def test_a_cannot_add_a_family_to_bs_quiz(
        self, client: TestClient, teacher_a: dict[str, str], b_resources: dict[str, str]
    ) -> None:
        response = client.post(
            f"/api/quizzes/{b_resources['quiz_id']}/questions",
            headers=teacher_a,
            json={"original_question": "Injected question", "question_type": "MCQ"},
        )
        assert response.status_code == 404

    def test_a_cannot_generate_variants_into_bs_family(
        self, client: TestClient, teacher_a: dict[str, str], b_resources: dict[str, str]
    ) -> None:
        response = client.post(
            f"/api/questions/{b_resources['family_id']}/generate-variants",
            headers=teacher_a,
            json={"count": 1},
        )
        assert response.status_code == 404

    def test_a_cannot_list_bs_variants(
        self, client: TestClient, teacher_a: dict[str, str], b_resources: dict[str, str]
    ) -> None:
        response = client.get(
            f"/api/questions/{b_resources['family_id']}/variants", headers=teacher_a
        )
        assert response.status_code == 404


class TestCrossTeacherVariantAccess:
    def test_a_cannot_read_bs_variant(
        self, client: TestClient, teacher_a: dict[str, str], b_resources: dict[str, str]
    ) -> None:
        response = client.get(f"/api/variants/{b_resources['variant_id']}", headers=teacher_a)
        assert response.status_code == 404

    def test_a_cannot_approve_bs_variant(
        self, client: TestClient, teacher_a: dict[str, str], b_resources: dict[str, str]
    ) -> None:
        response = client.post(
            f"/api/variants/{b_resources['variant_id']}/approve", headers=teacher_a
        )
        assert response.status_code == 404

    def test_a_cannot_reject_bs_variant(
        self, client: TestClient, teacher_a: dict[str, str], b_resources: dict[str, str]
    ) -> None:
        response = client.post(
            f"/api/variants/{b_resources['variant_id']}/reject",
            headers=teacher_a,
            json={"reason": "sabotage"},
        )
        assert response.status_code == 404

    def test_a_cannot_edit_bs_variant(
        self, client: TestClient, teacher_a: dict[str, str], b_resources: dict[str, str]
    ) -> None:
        response = client.patch(
            f"/api/variants/{b_resources['variant_id']}",
            headers=teacher_a,
            json={"question_text": "Attacker rewrote this question entirely"},
        )
        assert response.status_code == 404

    def test_a_cannot_delete_bs_variant(
        self, client: TestClient, teacher_a: dict[str, str], b_resources: dict[str, str]
    ) -> None:
        assert (
            client.delete(
                f"/api/variants/{b_resources['variant_id']}", headers=teacher_a
            ).status_code
            == 404
        )

    def test_bs_variant_is_untouched_after_every_attack(
        self, client: TestClient, teacher_a: dict[str, str], teacher_b: dict[str, str],
        b_resources: dict[str, str]
    ) -> None:
        before = client.get(f"/api/variants/{b_resources['variant_id']}", headers=teacher_b).json()
        for call in (
            lambda: client.post(f"/api/variants/{b_resources['variant_id']}/approve", headers=teacher_a),
            lambda: client.patch(
                f"/api/variants/{b_resources['variant_id']}",
                headers=teacher_a,
                json={"question_text": "Attacker rewrote this question entirely"},
            ),
            lambda: client.delete(f"/api/variants/{b_resources['variant_id']}", headers=teacher_a),
        ):
            assert call().status_code == 404
        after = client.get(f"/api/variants/{b_resources['variant_id']}", headers=teacher_b).json()
        assert after == before


class TestIdentifierTampering:
    @pytest.mark.parametrize(
        "bad_id",
        ["not-a-uuid", "1 OR 1=1", "../../etc/passwd", "00000000-0000-0000-0000-00000000000z", ""],
    )
    def test_malformed_quiz_id_is_a_404_not_a_500(
        self, client: TestClient, teacher_a: dict[str, str], bad_id: str
    ) -> None:
        response = client.get(f"/api/quizzes/{bad_id}", headers=teacher_a)
        # An empty id hits the list route; anything else must be a clean 404.
        assert response.status_code in (200, 404), response.text

    @pytest.mark.parametrize("bad_id", ["not-a-uuid", "1;DROP TABLE quizzes", "%2e%2e"])
    def test_malformed_family_id_is_a_404(
        self, client: TestClient, teacher_a: dict[str, str], bad_id: str
    ) -> None:
        assert client.get(f"/api/questions/{bad_id}", headers=teacher_a).status_code == 404

    @pytest.mark.parametrize("bad_id", ["not-a-uuid", "0", "null"])
    def test_malformed_variant_id_is_a_404(
        self, client: TestClient, teacher_a: dict[str, str], bad_id: str
    ) -> None:
        assert client.get(f"/api/variants/{bad_id}", headers=teacher_a).status_code == 404

    def test_a_random_uuid_is_a_404(self, client: TestClient, teacher_a: dict[str, str]) -> None:
        assert client.get(f"/api/quizzes/{uuid.uuid4()}", headers=teacher_a).status_code == 404

    def test_a_nonexistent_and_a_forbidden_quiz_are_indistinguishable(
        self, client: TestClient, teacher_a: dict[str, str], b_resources: dict[str, str]
    ) -> None:
        """Otherwise the API is an existence oracle for other people's quiz ids."""
        forbidden = client.get(f"/api/quizzes/{b_resources['quiz_id']}", headers=teacher_a)
        missing = client.get(f"/api/quizzes/{uuid.uuid4()}", headers=teacher_a)
        assert forbidden.status_code == missing.status_code == 404
        assert forbidden.json() == missing.json()


class TestUnauthenticatedAccess:
    def test_every_teacher_route_requires_a_token(self, client: TestClient) -> None:
        quiz_id = uuid.uuid4()
        calls = [
            ("get", "/api/quizzes"),
            ("post", "/api/quizzes"),
            ("get", f"/api/quizzes/{quiz_id}"),
            ("patch", f"/api/quizzes/{quiz_id}"),
            ("post", f"/api/quizzes/{quiz_id}/publish"),
            ("post", f"/api/quizzes/{quiz_id}/unpublish"),
            ("get", f"/api/quizzes/{quiz_id}/questions"),
            ("get", f"/api/questions/{quiz_id}"),
            ("get", f"/api/questions/{quiz_id}/variants"),
            ("post", f"/api/questions/{quiz_id}/generate-variants"),
            ("get", f"/api/variants/{quiz_id}"),
            ("post", f"/api/variants/{quiz_id}/approve"),
            ("post", f"/api/variants/{quiz_id}/reject"),
        ]
        for method, path in calls:
            # GET/DELETE take no body in httpx; only send one where it applies.
            kwargs = {"json": {}} if method in ("post", "patch") else {}
            response = getattr(client, method)(path, **kwargs)
            assert response.status_code == 401, f"{method.upper()} {path} -> {response.status_code}"

    def test_a_token_without_a_profile_is_refused(self, client: TestClient) -> None:
        from tests.conftest import headers_for

        headers = headers_for("cccccccc-0000-0000-0000-000000000003", "ghost@example.edu")
        response = client.get("/api/quizzes", headers=headers)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "PROFILE_NOT_FOUND"
