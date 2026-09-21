"""Adversarial tests for the student exam engine.

The question each test asks: with DevTools open and a proxy in front of the
browser, can a student change something the server ultimately trusts?

Every test is named for the attack it attempts.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import generate_attempt_token, hash_attempt_token
from app.db.models import Attempt, AttemptStatus, Quiz, QuestionVariant, VariantStatus
from tests.test_ownership import make_family, make_quiz


# --- helpers ---------------------------------------------------------------


def publish(
    client: TestClient, headers: dict[str, str], *, questions: int = 2, variants: int = 2, **quiz_kw
) -> dict:
    quiz = make_quiz(client, headers, question_count=questions, **quiz_kw)
    for _ in range(questions):
        family = make_family(client, headers, quiz["id"])
        body = client.post(
            f"/api/questions/{family['id']}/generate-variants", headers=headers, json={"count": 4}
        ).json()
        for variant in body["variants"][:variants]:
            client.post(f"/api/variants/{variant['id']}/approve", headers=headers)
    response = client.post(f"/api/quizzes/{quiz['id']}/publish", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def start(client: TestClient, code: str, email: str = "asha@uni.edu", name: str = "Asha") -> dict:
    response = client.post(
        f"/api/public/quizzes/{code}/attempts",
        json={"student_name": name, "student_email": email},
    )
    assert response.status_code == 201, response.text
    return response.json()


def auth(token: str) -> dict[str, str]:
    return {"X-Attempt-Token": token}


@pytest.fixture
def exam(client: TestClient, teacher_a: dict[str, str]) -> dict:
    """A published quiz with one started attempt."""
    quiz = publish(client, teacher_a)
    session = start(client, quiz["public_code"])
    questions = client.get(
        f"/api/student/attempts/{session['attempt_id']}/questions",
        headers=auth(session["attempt_token"]),
    ).json()["questions"]
    return {**session, "public_code": quiz["public_code"], "quiz_id": quiz["id"],
            "questions": questions}


# --- Public access ---------------------------------------------------------


class TestPublicAccess:
    def test_an_unknown_code_is_a_404(self, client: TestClient) -> None:
        assert client.get("/api/public/quizzes/ZZZZZZ").status_code == 404

    def test_an_unpublished_quiz_is_indistinguishable_from_a_missing_one(
        self, client: TestClient, teacher_a: dict[str, str]
    ) -> None:
        """A guessed code must not reveal that a draft is being prepared."""
        quiz = publish(client, teacher_a)
        client.post(f"/api/quizzes/{quiz['id']}/unpublish", headers=teacher_a)

        hidden = client.get(f"/api/public/quizzes/{quiz['public_code']}")
        missing = client.get("/api/public/quizzes/ZZZZZZ")
        assert hidden.status_code == missing.status_code == 404
        assert hidden.json() == missing.json()

    def test_a_closed_quiz_is_not_publicly_readable(
        self, client: TestClient, teacher_a: dict[str, str]
    ) -> None:
        quiz = publish(client, teacher_a)
        client.post(f"/api/quizzes/{quiz['id']}/close", headers=teacher_a)
        assert client.get(f"/api/public/quizzes/{quiz['public_code']}").status_code == 404

    def test_the_public_payload_contains_no_question_content(
        self, client: TestClient, teacher_a: dict[str, str]
    ) -> None:
        quiz = publish(client, teacher_a)
        body = client.get(f"/api/public/quizzes/{quiz['public_code']}").text
        for forbidden in ("correct_option", "options", "question_text", "variant", "explanation"):
            assert forbidden not in body, f"public payload leaks {forbidden}"

    def test_the_public_payload_does_not_expose_internal_ids(
        self, client: TestClient, teacher_a: dict[str, str]
    ) -> None:
        quiz = publish(client, teacher_a)
        body = client.get(f"/api/public/quizzes/{quiz['public_code']}").json()
        assert "id" not in body and "owner_id" not in body

    @pytest.mark.parametrize(
        "hostile", ["../../etc/passwd", "'; DROP TABLE quizzes;--", "%00", "A" * 200]
    )
    def test_hostile_public_codes_are_refused_cleanly(
        self, client: TestClient, hostile: str
    ) -> None:
        assert client.get(f"/api/public/quizzes/{hostile}").status_code in (404, 422)

    def test_cannot_start_an_attempt_on_an_unpublished_quiz(
        self, client: TestClient, teacher_a: dict[str, str]
    ) -> None:
        quiz = publish(client, teacher_a)
        client.post(f"/api/quizzes/{quiz['id']}/unpublish", headers=teacher_a)
        response = client.post(
            f"/api/public/quizzes/{quiz['public_code']}/attempts",
            json={"student_name": "Asha", "student_email": "asha@uni.edu"},
        )
        assert response.status_code == 404


# --- Attempt authorization -------------------------------------------------


class TestCrossStudentAccess:
    @pytest.fixture
    def two_students(self, client: TestClient, teacher_a: dict[str, str]) -> dict:
        quiz = publish(client, teacher_a)
        a = start(client, quiz["public_code"], email="a@uni.edu", name="A")
        b = start(client, quiz["public_code"], email="b@uni.edu", name="B")
        return {"a": a, "b": b}

    def test_a_cannot_read_bs_attempt(self, client: TestClient, two_students: dict) -> None:
        response = client.get(
            f"/api/student/attempts/{two_students['b']['attempt_id']}",
            headers=auth(two_students["a"]["attempt_token"]),
        )
        assert response.status_code == 404

    def test_a_cannot_read_bs_questions(self, client: TestClient, two_students: dict) -> None:
        response = client.get(
            f"/api/student/attempts/{two_students['b']['attempt_id']}/questions",
            headers=auth(two_students["a"]["attempt_token"]),
        )
        assert response.status_code == 404

    def test_a_cannot_write_an_answer_into_bs_attempt(
        self, client: TestClient, two_students: dict
    ) -> None:
        b_questions = client.get(
            f"/api/student/attempts/{two_students['b']['attempt_id']}/questions",
            headers=auth(two_students["b"]["attempt_token"]),
        ).json()["questions"]
        response = client.post(
            f"/api/student/attempts/{two_students['b']['attempt_id']}/answers",
            headers=auth(two_students["a"]["attempt_token"]),
            json={
                "attempt_question_id": b_questions[0]["attempt_question_id"],
                "selected_answer": {"displayed_index": 0},
            },
        )
        assert response.status_code == 404

    def test_a_cannot_submit_bs_attempt(self, client: TestClient, two_students: dict) -> None:
        response = client.post(
            f"/api/student/attempts/{two_students['b']['attempt_id']}/submit",
            headers=auth(two_students["a"]["attempt_token"]),
            json={},
        )
        assert response.status_code == 404

    def test_a_cannot_report_integrity_events_against_b(
        self, client: TestClient, two_students: dict
    ) -> None:
        """Otherwise one student could auto-submit another out of the exam."""
        response = client.post(
            f"/api/student/attempts/{two_students['b']['attempt_id']}/integrity-events",
            headers=auth(two_students["a"]["attempt_token"]),
            json={"event_type": "FULLSCREEN_EXIT"},
        )
        assert response.status_code == 404

    def test_a_cannot_write_bs_question_into_their_own_attempt(
        self, client: TestClient, two_students: dict
    ) -> None:
        """A question id from another paper must not resolve in this attempt."""
        b_questions = client.get(
            f"/api/student/attempts/{two_students['b']['attempt_id']}/questions",
            headers=auth(two_students["b"]["attempt_token"]),
        ).json()["questions"]
        response = client.post(
            f"/api/student/attempts/{two_students['a']['attempt_id']}/answers",
            headers=auth(two_students["a"]["attempt_token"]),
            json={
                "attempt_question_id": b_questions[0]["attempt_question_id"],
                "selected_answer": {"displayed_index": 0},
            },
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "QUESTION_NOT_FOUND"

    def test_bs_attempt_is_unchanged_after_every_attack(
        self, client: TestClient, two_students: dict
    ) -> None:
        before = client.get(
            f"/api/student/attempts/{two_students['b']['attempt_id']}",
            headers=auth(two_students["b"]["attempt_token"]),
        ).json()
        for call in (
            lambda: client.post(
                f"/api/student/attempts/{two_students['b']['attempt_id']}/submit",
                headers=auth(two_students["a"]["attempt_token"]), json={}),
            lambda: client.post(
                f"/api/student/attempts/{two_students['b']['attempt_id']}/integrity-events",
                headers=auth(two_students["a"]["attempt_token"]),
                json={"event_type": "FULLSCREEN_EXIT"}),
        ):
            assert call().status_code == 404
        after = client.get(
            f"/api/student/attempts/{two_students['b']['attempt_id']}",
            headers=auth(two_students["b"]["attempt_token"]),
        ).json()
        assert after["status"] == before["status"]
        assert after["violation_count"] == before["violation_count"]


class TestTokenHandling:
    def test_no_token_is_refused(self, client: TestClient, exam: dict) -> None:
        assert client.get(f"/api/student/attempts/{exam['attempt_id']}").status_code == 401

    def test_an_unknown_token_is_refused(self, client: TestClient, exam: dict) -> None:
        response = client.get(
            f"/api/student/attempts/{exam['attempt_id']}", headers=auth(generate_attempt_token())
        )
        assert response.status_code == 404

    def test_a_truncated_token_is_refused(self, client: TestClient, exam: dict) -> None:
        response = client.get(
            f"/api/student/attempts/{exam['attempt_id']}",
            headers=auth(exam["attempt_token"][:-1]),
        )
        assert response.status_code == 404

    def test_the_token_hash_is_not_accepted_as_the_token(
        self, client: TestClient, exam: dict
    ) -> None:
        """Someone who reads the database still cannot act as the student."""
        response = client.get(
            f"/api/student/attempts/{exam['attempt_id']}",
            headers=auth(hash_attempt_token(exam["attempt_token"])),
        )
        assert response.status_code == 404

    @pytest.mark.parametrize("bad_id", ["not-a-uuid", "1 OR 1=1", "../../x", "0"])
    def test_malformed_attempt_ids_are_refused(
        self, client: TestClient, exam: dict, bad_id: str
    ) -> None:
        response = client.get(
            f"/api/student/attempts/{bad_id}", headers=auth(exam["attempt_token"])
        )
        assert response.status_code == 404

    def test_all_authorization_failures_look_identical(
        self, client: TestClient, exam: dict
    ) -> None:
        wrong_id = client.get(
            f"/api/student/attempts/{uuid.uuid4()}", headers=auth(exam["attempt_token"])
        )
        wrong_token = client.get(
            f"/api/student/attempts/{exam['attempt_id']}", headers=auth(generate_attempt_token())
        )
        assert wrong_id.status_code == wrong_token.status_code == 404
        assert wrong_id.json() == wrong_token.json()

    def test_the_token_is_never_echoed_back(self, client: TestClient, exam: dict) -> None:
        state = client.get(
            f"/api/student/attempts/{exam['attempt_id']}", headers=auth(exam["attempt_token"])
        )
        assert exam["attempt_token"] not in state.text


# --- One attempt per student ----------------------------------------------


class TestOneAttemptPolicy:
    def test_a_second_start_is_refused(self, client: TestClient, teacher_a: dict[str, str]) -> None:
        quiz = publish(client, teacher_a)
        start(client, quiz["public_code"], email="asha@uni.edu")
        response = client.post(
            f"/api/public/quizzes/{quiz['public_code']}/attempts",
            json={"student_name": "Asha", "student_email": "asha@uni.edu"},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "ATTEMPT_ALREADY_EXISTS"

    def test_email_case_does_not_create_a_second_attempt(
        self, client: TestClient, teacher_a: dict[str, str]
    ) -> None:
        quiz = publish(client, teacher_a)
        start(client, quiz["public_code"], email="asha@uni.edu")
        response = client.post(
            f"/api/public/quizzes/{quiz['public_code']}/attempts",
            json={"student_name": "Asha", "student_email": "ASHA@UNI.EDU"},
        )
        assert response.status_code == 409

    def test_opening_a_second_tab_does_not_create_a_second_attempt(
        self, client: TestClient, teacher_a: dict[str, str], db: Session
    ) -> None:
        quiz = publish(client, teacher_a)
        start(client, quiz["public_code"], email="asha@uni.edu")
        client.post(
            f"/api/public/quizzes/{quiz['public_code']}/attempts",
            json={"student_name": "Asha", "student_email": "asha@uni.edu"},
        )
        count = len(db.scalars(select(Attempt).where(Attempt.student_email == "asha@uni.edu")).all())
        assert count == 1


# --- Assignment ------------------------------------------------------------


class TestAssignment:
    def test_a_refresh_returns_the_identical_paper(self, client: TestClient, exam: dict) -> None:
        """Questions and option order must not be regenerated on reload."""
        again = client.get(
            f"/api/student/attempts/{exam['attempt_id']}/questions",
            headers=auth(exam["attempt_token"]),
        ).json()["questions"]
        assert again == exam["questions"]

    def test_the_paper_has_the_configured_question_count(
        self, client: TestClient, exam: dict
    ) -> None:
        assert len(exam["questions"]) == 2

    def test_no_family_appears_twice_on_one_paper(
        self, client: TestClient, teacher_a: dict[str, str], db: Session
    ) -> None:
        from app.db.models import AttemptQuestion, QuestionFamily

        quiz = publish(client, teacher_a, questions=3, variants=3)
        session = start(client, quiz["public_code"])
        rows = db.scalars(
            select(AttemptQuestion).where(AttemptQuestion.attempt_id == uuid.UUID(session["attempt_id"]))
        ).all()
        families = [
            db.get(QuestionVariant, row.variant_id).family_id for row in rows
        ]
        assert len(families) == len(set(families))
        assert db.scalars(select(QuestionFamily)).all()  # sanity

    def test_only_assignable_variants_are_served(
        self, client: TestClient, teacher_a: dict[str, str], db: Session
    ) -> None:
        """Rejected, unapproved and superseded variants must never appear."""
        from app.db.models import AttemptQuestion

        quiz = publish(client, teacher_a, questions=2, variants=2)
        session = start(client, quiz["public_code"])
        rows = db.scalars(
            select(AttemptQuestion).where(
                AttemptQuestion.attempt_id == uuid.UUID(session["attempt_id"])
            )
        ).all()
        for row in rows:
            variant = db.get(QuestionVariant, row.variant_id)
            assert variant.validation_status in (VariantStatus.APPROVED, VariantStatus.PUBLISHED)
            assert variant.superseded_by_id is None

    def test_different_students_can_receive_different_papers(
        self, client: TestClient, teacher_a: dict[str, str]
    ) -> None:
        """The same link must not hand everyone the identical exam."""
        quiz = publish(client, teacher_a, questions=3, variants=4)
        seen = set()
        for index in range(12):
            session = start(client, quiz["public_code"], email=f"s{index}@uni.edu")
            questions = client.get(
                f"/api/student/attempts/{session['attempt_id']}/questions",
                headers=auth(session["attempt_token"]),
            ).json()["questions"]
            seen.add(tuple(q["question_text"] for q in questions))
        assert len(seen) > 1, "every student received the same paper"

    def test_the_client_cannot_choose_its_own_seed_or_questions(
        self, client: TestClient, teacher_a: dict[str, str]
    ) -> None:
        quiz = publish(client, teacher_a)
        response = client.post(
            f"/api/public/quizzes/{quiz['public_code']}/attempts",
            json={
                "student_name": "Asha",
                "student_email": "asha@uni.edu",
                "seed": 1234,
                "question_ids": ["whatever"],
            },
        )
        assert response.status_code == 422, "extra fields must be rejected, not ignored"


class TestHistoricalImmutability:
    """A served paper is historical assessment data."""

    def test_superseding_a_variant_does_not_change_a_running_attempt(
        self, client: TestClient, teacher_a: dict[str, str], db: Session
    ) -> None:
        from app.db.models import AttemptQuestion

        quiz = publish(client, teacher_a, questions=1, variants=1)
        session = start(client, quiz["public_code"])
        before = client.get(
            f"/api/student/attempts/{session['attempt_id']}/questions",
            headers=auth(session["attempt_token"]),
        ).json()["questions"]

        # Teacher edits the exact variant this student was served.
        row = db.scalars(
            select(AttemptQuestion).where(
                AttemptQuestion.attempt_id == uuid.UUID(session["attempt_id"])
            )
        ).first()
        edit = client.patch(
            f"/api/variants/{row.variant_id}",
            headers=teacher_a,
            json={"question_text": "A completely different question after the fact?"},
        )
        assert edit.status_code == 200
        assert edit.json()["id"] != str(row.variant_id), "must supersede, not rewrite"

        after = client.get(
            f"/api/student/attempts/{session['attempt_id']}/questions",
            headers=auth(session["attempt_token"]),
        ).json()["questions"]
        assert after == before, "a running attempt must not change underneath the student"

    def test_rejecting_a_served_variant_does_not_change_the_attempt(
        self, client: TestClient, teacher_a: dict[str, str], db: Session
    ) -> None:
        from app.db.models import AttemptQuestion

        quiz = publish(client, teacher_a, questions=1, variants=1)
        session = start(client, quiz["public_code"])
        before = client.get(
            f"/api/student/attempts/{session['attempt_id']}/questions",
            headers=auth(session["attempt_token"]),
        ).json()["questions"]

        row = db.scalars(
            select(AttemptQuestion).where(
                AttemptQuestion.attempt_id == uuid.UUID(session["attempt_id"])
            )
        ).first()
        client.post(f"/api/variants/{row.variant_id}/reject", headers=teacher_a, json={})

        after = client.get(
            f"/api/student/attempts/{session['attempt_id']}/questions",
            headers=auth(session["attempt_token"]),
        ).json()["questions"]
        assert after == before

    def test_a_served_variant_cannot_be_deleted_by_the_teacher(
        self, client: TestClient, teacher_a: dict[str, str], db: Session
    ) -> None:
        from app.db.models import AttemptQuestion

        quiz = publish(client, teacher_a, questions=1, variants=1)
        session = start(client, quiz["public_code"])
        row = db.scalars(
            select(AttemptQuestion).where(
                AttemptQuestion.attempt_id == uuid.UUID(session["attempt_id"])
            )
        ).first()
        response = client.delete(f"/api/variants/{row.variant_id}", headers=teacher_a)
        assert response.status_code == 409


# --- Timer -----------------------------------------------------------------


class TestTimerAuthority:
    def _expire(self, db: Session, attempt_id: str) -> None:
        """Push the deadline well past the clock-skew grace window.

        ATTEMPT_GRACE_SECONDS deliberately accepts an answer that was in flight
        at the buzzer, so expiring by one second would not expire the attempt.
        """
        attempt = db.get(Attempt, uuid.UUID(attempt_id))
        attempt.expires_at = attempt.started_at - timedelta(hours=1)
        db.commit()

    def test_an_expired_attempt_refuses_answers(
        self, client: TestClient, exam: dict, db: Session
    ) -> None:
        self._expire(db, exam["attempt_id"])
        response = client.post(
            f"/api/student/attempts/{exam['attempt_id']}/answers",
            headers=auth(exam["attempt_token"]),
            json={
                "attempt_question_id": exam["questions"][0]["attempt_question_id"],
                "selected_answer": {"displayed_index": 1},
            },
        )
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "ATTEMPT_ALREADY_FINALIZED"

    def test_expiry_finalizes_the_attempt_on_the_next_request(
        self, client: TestClient, exam: dict, db: Session
    ) -> None:
        """A student who closes the laptop still gets a graded record."""
        self._expire(db, exam["attempt_id"])
        state = client.get(
            f"/api/student/attempts/{exam['attempt_id']}", headers=auth(exam["attempt_token"])
        ).json()
        assert state["status"] == "TIME_EXPIRED"
        assert state["seconds_remaining"] == 0

    def test_a_client_supplied_expiry_is_ignored(
        self, client: TestClient, exam: dict, db: Session
    ) -> None:
        """There is no field to extend the exam, and sending one is a 422."""
        self._expire(db, exam["attempt_id"])
        response = client.post(
            f"/api/student/attempts/{exam['attempt_id']}/answers",
            headers=auth(exam["attempt_token"]),
            json={
                "attempt_question_id": exam["questions"][0]["attempt_question_id"],
                "selected_answer": {"displayed_index": 1},
                "expires_at": "2099-01-01T00:00:00Z",
                "seconds_remaining": 9999,
            },
        )
        assert response.status_code == 422

    def test_submitting_an_expired_attempt_yields_time_expired(
        self, client: TestClient, exam: dict, db: Session
    ) -> None:
        self._expire(db, exam["attempt_id"])
        response = client.post(
            f"/api/student/attempts/{exam['attempt_id']}/submit",
            headers=auth(exam["attempt_token"]),
            json={},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "TIME_EXPIRED"

    def test_seconds_remaining_is_computed_from_the_server_clock(
        self, client: TestClient, exam: dict
    ) -> None:
        state = client.get(
            f"/api/student/attempts/{exam['attempt_id']}", headers=auth(exam["attempt_token"])
        ).json()
        assert 0 < state["seconds_remaining"] <= 600
        assert state["server_time"] and state["expires_at"]


# --- Grading ---------------------------------------------------------------


class TestGradingCannotBeInfluenced:
    @pytest.mark.parametrize(
        "payload",
        [
            {"displayed_index": 0, "is_correct": True},
            {"displayed_index": 0, "score": 100},
            {"displayed_index": 0, "percentage": 100},
            {"displayed_index": 0, "correct_option": 0},
            {"displayed_index": 0, "grade": "A"},
        ],
    )
    def test_grading_fields_in_an_answer_are_rejected(
        self, client: TestClient, exam: dict, payload: dict
    ) -> None:
        response = client.post(
            f"/api/student/attempts/{exam['attempt_id']}/answers",
            headers=auth(exam["attempt_token"]),
            json={
                "attempt_question_id": exam["questions"][0]["attempt_question_id"],
                "selected_answer": payload,
            },
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "GRADING_FIELD_REJECTED"

    def test_an_out_of_range_option_is_rejected(self, client: TestClient, exam: dict) -> None:
        response = client.post(
            f"/api/student/attempts/{exam['attempt_id']}/answers",
            headers=auth(exam["attempt_token"]),
            json={
                "attempt_question_id": exam["questions"][0]["attempt_question_id"],
                "selected_answer": {"displayed_index": 99},
            },
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "OPTION_OUT_OF_RANGE"

    @pytest.mark.parametrize("bad", [{"displayed_index": "0"}, {"displayed_index": True}, {}])
    def test_malformed_answers_are_rejected(
        self, client: TestClient, exam: dict, bad: dict
    ) -> None:
        response = client.post(
            f"/api/student/attempts/{exam['attempt_id']}/answers",
            headers=auth(exam["attempt_token"]),
            json={
                "attempt_question_id": exam["questions"][0]["attempt_question_id"],
                "selected_answer": bad,
            },
        )
        assert response.status_code == 400

    def test_the_answer_response_never_reveals_correctness(
        self, client: TestClient, exam: dict
    ) -> None:
        """Otherwise the exam becomes an oracle a student can brute-force."""
        response = client.post(
            f"/api/student/attempts/{exam['attempt_id']}/answers",
            headers=auth(exam["attempt_token"]),
            json={
                "attempt_question_id": exam["questions"][0]["attempt_question_id"],
                "selected_answer": {"displayed_index": 0},
            },
        )
        body = response.text
        assert "is_correct" not in body and "correct" not in body.replace("correct_option", "")

    def test_is_correct_stays_null_until_finalization(
        self, client: TestClient, exam: dict, db: Session
    ) -> None:
        from app.db.models import Answer

        client.post(
            f"/api/student/attempts/{exam['attempt_id']}/answers",
            headers=auth(exam["attempt_token"]),
            json={
                "attempt_question_id": exam["questions"][0]["attempt_question_id"],
                "selected_answer": {"displayed_index": 0},
            },
        )
        answer = db.scalars(select(Answer)).first()
        assert answer is not None and answer.is_correct is None

    def test_the_score_is_computed_from_the_stored_key(
        self, client: TestClient, teacher_a: dict[str, str], db: Session
    ) -> None:
        """Answer every question correctly by reading the key server-side."""
        from app.db.models import AttemptQuestion

        quiz = publish(client, teacher_a, questions=2, variants=1)
        quiz_row = db.get(Quiz, uuid.UUID(quiz["id"]))
        quiz_row.show_score_after_submission = True
        db.commit()

        session = start(client, quiz["public_code"])
        questions = client.get(
            f"/api/student/attempts/{session['attempt_id']}/questions",
            headers=auth(session["attempt_token"]),
        ).json()["questions"]

        for question in questions:
            row = db.get(AttemptQuestion, uuid.UUID(question["attempt_question_id"]))
            variant = db.get(QuestionVariant, row.variant_id)
            # Map the canonical correct option back to its displayed position.
            order = row.options_order_json or list(range(len(variant.options_json)))
            displayed = order.index(variant.correct_option)
            client.post(
                f"/api/student/attempts/{session['attempt_id']}/answers",
                headers=auth(session["attempt_token"]),
                json={
                    "attempt_question_id": question["attempt_question_id"],
                    "selected_answer": {"displayed_index": displayed},
                },
            )

        result = client.post(
            f"/api/student/attempts/{session['attempt_id']}/submit",
            headers=auth(session["attempt_token"]),
            json={},
        ).json()
        assert result["score"] == 2.0
        assert result["percentage"] == 100.0


# --- Integrity -------------------------------------------------------------


class TestIntegrityPolicy:
    def test_a_client_supplied_violation_count_is_ignored(
        self, client: TestClient, exam: dict
    ) -> None:
        response = client.post(
            f"/api/student/attempts/{exam['attempt_id']}/integrity-events",
            headers=auth(exam["attempt_token"]),
            json={"event_type": "TAB_HIDDEN", "violation_count": 0, "severity": 0},
        )
        assert response.status_code == 422, "unknown fields must be rejected"

    def test_the_server_assigns_severity(self, client: TestClient, exam: dict) -> None:
        response = client.post(
            f"/api/student/attempts/{exam['attempt_id']}/integrity-events",
            headers=auth(exam["attempt_token"]),
            json={"event_type": "TAB_HIDDEN"},
        ).json()
        assert response["violation_total"] == 1

    def test_informational_events_do_not_count(self, client: TestClient, exam: dict) -> None:
        """A dropped connection is not misconduct."""
        for event in ("TAB_VISIBLE", "WINDOW_FOCUS", "NETWORK_DISCONNECTED", "RIGHT_CLICK"):
            response = client.post(
                f"/api/student/attempts/{exam['attempt_id']}/integrity-events",
                headers=auth(exam["attempt_token"]),
                json={"event_type": event},
            ).json()
            assert response["violation_total"] == 0, f"{event} must be informational"
            assert response["integrity_status"] == "CLEAN"

    def test_an_invalid_event_type_is_rejected(self, client: TestClient, exam: dict) -> None:
        response = client.post(
            f"/api/student/attempts/{exam['attempt_id']}/integrity-events",
            headers=auth(exam["attempt_token"]),
            json={"event_type": "STUDENT_WAS_PERFECT"},
        )
        assert response.status_code == 422

    @pytest.mark.parametrize("event", ["TIME_EXPIRED", "AUTO_SUBMISSION_TRIGGERED"])
    def test_a_client_cannot_fabricate_server_only_events(
        self, client: TestClient, exam: dict, event: str
    ) -> None:
        """Forging these would corrupt the audit trail."""
        response = client.post(
            f"/api/student/attempts/{exam['attempt_id']}/integrity-events",
            headers=auth(exam["attempt_token"]),
            json={"event_type": event},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "SERVER_ONLY_EVENT"

    def test_oversized_metadata_is_bounded_not_rejected(
        self, client: TestClient, exam: dict, db: Session
    ) -> None:
        from app.db.models import IntegrityEvent

        response = client.post(
            f"/api/student/attempts/{exam['attempt_id']}/integrity-events",
            headers=auth(exam["attempt_token"]),
            json={"event_type": "TAB_HIDDEN", "metadata": {"note": "x" * 5000}},
        )
        assert response.status_code == 200
        event = db.scalars(select(IntegrityEvent)).first()
        assert len(event.metadata_json["note"]) <= 500

    def test_the_threshold_triggers_a_server_side_auto_submit(
        self, client: TestClient, teacher_a: dict[str, str]
    ) -> None:
        quiz = publish(client, teacher_a, max_integrity_violations=2)
        session = start(client, quiz["public_code"])
        headers = auth(session["attempt_token"])

        for _ in range(2):
            body = client.post(
                f"/api/student/attempts/{session['attempt_id']}/integrity-events",
                headers=headers, json={"event_type": "TAB_HIDDEN"},
            ).json()
            assert body["auto_submitted"] is False

        third = client.post(
            f"/api/student/attempts/{session['attempt_id']}/integrity-events",
            headers=headers, json={"event_type": "FULLSCREEN_EXIT"},
        ).json()
        assert third["auto_submitted"] is True
        assert third["attempt_status"] == "AUTO_SUBMITTED"
        assert third["integrity_status"] == "FLAGGED"

    def test_an_auto_submitted_attempt_refuses_further_answers(
        self, client: TestClient, teacher_a: dict[str, str]
    ) -> None:
        quiz = publish(client, teacher_a, max_integrity_violations=0)
        session = start(client, quiz["public_code"])
        headers = auth(session["attempt_token"])
        questions = client.get(
            f"/api/student/attempts/{session['attempt_id']}/questions", headers=headers
        ).json()["questions"]

        client.post(
            f"/api/student/attempts/{session['attempt_id']}/integrity-events",
            headers=headers, json={"event_type": "TAB_HIDDEN"},
        )
        response = client.post(
            f"/api/student/attempts/{session['attempt_id']}/answers",
            headers=headers,
            json={
                "attempt_question_id": questions[0]["attempt_question_id"],
                "selected_answer": {"displayed_index": 0},
            },
        )
        assert response.status_code == 409

    def test_integrity_events_after_finalization_are_refused(
        self, client: TestClient, exam: dict
    ) -> None:
        client.post(
            f"/api/student/attempts/{exam['attempt_id']}/submit",
            headers=auth(exam["attempt_token"]), json={},
        )
        response = client.post(
            f"/api/student/attempts/{exam['attempt_id']}/integrity-events",
            headers=auth(exam["attempt_token"]), json={"event_type": "TAB_HIDDEN"},
        )
        assert response.status_code == 409

    def test_the_violation_count_survives_finalization(
        self, client: TestClient, exam: dict
    ) -> None:
        """Submitting must not launder away recorded signals."""
        client.post(
            f"/api/student/attempts/{exam['attempt_id']}/integrity-events",
            headers=auth(exam["attempt_token"]), json={"event_type": "TAB_HIDDEN"},
        )
        result = client.post(
            f"/api/student/attempts/{exam['attempt_id']}/submit",
            headers=auth(exam["attempt_token"]), json={},
        ).json()
        assert result["violation_count"] == 1
        assert result["integrity_status"] == "WARNING"


# --- Submission and result -------------------------------------------------


class TestSubmissionAndResult:
    def test_submission_is_idempotent(self, client: TestClient, exam: dict) -> None:
        first = client.post(
            f"/api/student/attempts/{exam['attempt_id']}/submit",
            headers=auth(exam["attempt_token"]), json={},
        ).json()
        second = client.post(
            f"/api/student/attempts/{exam['attempt_id']}/submit",
            headers=auth(exam["attempt_token"]), json={},
        ).json()
        assert first["submitted_at"] == second["submitted_at"]
        assert first["status"] == second["status"]

    def test_a_submitted_attempt_cannot_be_reopened(self, client: TestClient, exam: dict) -> None:
        client.post(
            f"/api/student/attempts/{exam['attempt_id']}/submit",
            headers=auth(exam["attempt_token"]), json={},
        )
        response = client.post(
            f"/api/student/attempts/{exam['attempt_id']}/answers",
            headers=auth(exam["attempt_token"]),
            json={
                "attempt_question_id": exam["questions"][0]["attempt_question_id"],
                "selected_answer": {"displayed_index": 1},
            },
        )
        assert response.status_code == 409

    def test_a_token_may_read_its_own_finished_result(
        self, client: TestClient, exam: dict
    ) -> None:
        client.post(
            f"/api/student/attempts/{exam['attempt_id']}/submit",
            headers=auth(exam["attempt_token"]), json={},
        )
        response = client.get(
            f"/api/student/attempts/{exam['attempt_id']}/result",
            headers=auth(exam["attempt_token"]),
        )
        assert response.status_code == 200

    def test_the_result_is_hidden_before_submission(
        self, client: TestClient, exam: dict
    ) -> None:
        response = client.get(
            f"/api/student/attempts/{exam['attempt_id']}/result",
            headers=auth(exam["attempt_token"]),
        )
        assert response.status_code == 409

    def test_the_score_is_withheld_when_the_quiz_says_so(
        self, client: TestClient, teacher_a: dict[str, str]
    ) -> None:
        """Absent from the payload, not merely hidden by the UI."""
        quiz = publish(client, teacher_a, show_score_after_submission=False)
        session = start(client, quiz["public_code"])
        result = client.post(
            f"/api/student/attempts/{session['attempt_id']}/submit",
            headers=auth(session["attempt_token"]), json={},
        ).json()
        assert result["score_visible"] is False
        assert result["score"] is None
        assert result["max_score"] is None
        assert result["percentage"] is None

    def test_the_score_is_shown_when_the_quiz_allows_it(
        self, client: TestClient, teacher_a: dict[str, str]
    ) -> None:
        quiz = publish(client, teacher_a, show_score_after_submission=True)
        session = start(client, quiz["public_code"])
        result = client.post(
            f"/api/student/attempts/{session['attempt_id']}/submit",
            headers=auth(session["attempt_token"]), json={},
        ).json()
        assert result["score_visible"] is True
        assert result["max_score"] == 2.0


# --- Disclosure ------------------------------------------------------------


class TestStudentDisclosure:
    FORBIDDEN = (
        "correct_option", "numeric_answer", "explanation", "generation_config_json",
        "validation_errors_json", "approved_by_id", "content_hash", "validation_status",
        "options_order_json", "variant_id", "is_correct", "seed", "attempt_token_hash",
    )

    def test_no_student_endpoint_leaks_a_forbidden_field(
        self, client: TestClient, exam: dict
    ) -> None:
        headers = auth(exam["attempt_token"])
        client.post(
            f"/api/student/attempts/{exam['attempt_id']}/answers", headers=headers,
            json={"attempt_question_id": exam["questions"][0]["attempt_question_id"],
                  "selected_answer": {"displayed_index": 0}},
        )
        client.post(
            f"/api/student/attempts/{exam['attempt_id']}/integrity-events",
            headers=headers, json={"event_type": "TAB_HIDDEN"},
        )
        bodies = [
            client.get(f"/api/public/quizzes/{exam['public_code']}").text,
            client.get(f"/api/student/attempts/{exam['attempt_id']}", headers=headers).text,
            client.get(f"/api/student/attempts/{exam['attempt_id']}/questions", headers=headers).text,
            client.post(f"/api/student/attempts/{exam['attempt_id']}/submit",
                        headers=headers, json={}).text,
            client.get(f"/api/student/attempts/{exam['attempt_id']}/result", headers=headers).text,
        ]
        for body in bodies:
            for field in self.FORBIDDEN:
                assert field not in body, f"student response leaks {field}"

    def test_every_student_schema_is_free_of_forbidden_fields(self) -> None:
        from app.schemas.student import FORBIDDEN_STUDENT_FIELDS, STUDENT_RESPONSE_MODELS

        for model in STUDENT_RESPONSE_MODELS:
            leaked = set(model.model_fields) & FORBIDDEN_STUDENT_FIELDS
            assert not leaked, f"{model.__name__} exposes {sorted(leaked)}"

    def test_the_option_permutation_is_never_sent(self, client: TestClient, exam: dict) -> None:
        """Without it, a replayed index tells an attacker nothing about the key."""
        body = client.get(
            f"/api/student/attempts/{exam['attempt_id']}/questions",
            headers=auth(exam["attempt_token"]),
        ).text
        assert "options_order" not in body


class TestEveryAnswerIsGraded:
    """Regression: answers written earlier in a session were silently ignored.

    Finalization used to grade through the cached ``attempt.questions``
    relationship. When the answers were written after that collection loaded,
    each question's ``answer`` link was stale, grade_attempt saw None, and a
    genuinely submitted answer scored zero. A student losing marks for work
    they did is the worst failure this engine can have.
    """

    def test_every_submitted_answer_receives_a_verdict(
        self, client: TestClient, teacher_a: dict[str, str], db: Session
    ) -> None:
        from app.db.models import Answer

        quiz = publish(client, teacher_a, questions=3, variants=1,
                       show_score_after_submission=True)
        session = start(client, quiz["public_code"])
        headers = auth(session["attempt_token"])
        questions = client.get(
            f"/api/student/attempts/{session['attempt_id']}/questions", headers=headers
        ).json()["questions"]

        for question in questions:
            client.post(
                f"/api/student/attempts/{session['attempt_id']}/answers",
                headers=headers,
                json={"attempt_question_id": question["attempt_question_id"],
                      "selected_answer": {"displayed_index": 0}},
            )

        client.post(
            f"/api/student/attempts/{session['attempt_id']}/submit", headers=headers, json={}
        )

        verdicts = [row.is_correct for row in db.scalars(select(Answer)).all()]
        assert len(verdicts) == 3
        assert all(v is not None for v in verdicts), (
            "every submitted answer must be graded, not skipped as unanswered"
        )

    def test_answering_every_question_correctly_scores_full_marks(
        self, client: TestClient, teacher_a: dict[str, str], db: Session
    ) -> None:
        from app.db.models import AttemptQuestion

        quiz = publish(client, teacher_a, questions=3, variants=1,
                       show_score_after_submission=True)
        session = start(client, quiz["public_code"])
        headers = auth(session["attempt_token"])
        questions = client.get(
            f"/api/student/attempts/{session['attempt_id']}/questions", headers=headers
        ).json()["questions"]

        for question in questions:
            row = db.get(AttemptQuestion, uuid.UUID(question["attempt_question_id"]))
            variant = db.get(QuestionVariant, row.variant_id)
            order = row.options_order_json or list(range(len(variant.options_json)))
            client.post(
                f"/api/student/attempts/{session['attempt_id']}/answers",
                headers=headers,
                json={"attempt_question_id": question["attempt_question_id"],
                      "selected_answer": {"displayed_index": order.index(variant.correct_option)}},
            )

        result = client.post(
            f"/api/student/attempts/{session['attempt_id']}/submit", headers=headers, json={}
        ).json()
        assert result["score"] == 3.0
        assert result["max_score"] == 3.0
        assert result["percentage"] == 100.0
