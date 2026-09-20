"""Variant lifecycle transitions, versioning and immutability.

The rule this file exists to protect: a question already used by an attempt
must never silently change underneath that attempt.
"""

from __future__ import annotations

import itertools

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.models import VariantStatus
from app.services.variant_lifecycle import (
    LEGAL_TRANSITIONS,
    IllegalTransition,
    assert_transition,
    can_transition,
    transition,
)
from tests.test_ownership import make_family, make_quiz


def generate_one(client: TestClient, headers: dict[str, str], family_id: str) -> dict:
    response = client.post(
        f"/api/questions/{family_id}/generate-variants", headers=headers, json={"count": 1}
    )
    assert response.status_code == 200, response.text
    return response.json()["variants"][0]


@pytest.fixture
def variant(client: TestClient, teacher_a: dict[str, str]) -> dict:
    quiz = make_quiz(client, teacher_a)
    family = make_family(client, teacher_a, quiz["id"])
    return {**generate_one(client, teacher_a, family["id"]),
            "_quiz_id": quiz["id"], "_family_id": family["id"]}


class TestTransitionTable:
    def test_every_state_has_an_entry(self) -> None:
        assert set(LEGAL_TRANSITIONS) == set(VariantStatus)

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            (VariantStatus.GENERATED, VariantStatus.VALIDATED),
            (VariantStatus.GENERATED, VariantStatus.REJECTED),
            (VariantStatus.VALIDATED, VariantStatus.APPROVED),
            (VariantStatus.VALIDATED, VariantStatus.REJECTED),
            (VariantStatus.APPROVED, VariantStatus.PUBLISHED),
            (VariantStatus.APPROVED, VariantStatus.REJECTED),
            (VariantStatus.PUBLISHED, VariantStatus.REJECTED),
            (VariantStatus.REJECTED, VariantStatus.VALIDATED),
        ],
    )
    def test_legal_transitions_are_allowed(
        self, current: VariantStatus, target: VariantStatus
    ) -> None:
        assert can_transition(current, target)

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            # Skipping review entirely: the transition that must never exist.
            (VariantStatus.GENERATED, VariantStatus.APPROVED),
            (VariantStatus.GENERATED, VariantStatus.PUBLISHED),
            (VariantStatus.VALIDATED, VariantStatus.PUBLISHED),
            # Going backwards.
            (VariantStatus.APPROVED, VariantStatus.VALIDATED),
            (VariantStatus.APPROVED, VariantStatus.GENERATED),
            (VariantStatus.PUBLISHED, VariantStatus.APPROVED),
            (VariantStatus.PUBLISHED, VariantStatus.VALIDATED),
            # Rejected content cannot leap straight back to approved.
            (VariantStatus.REJECTED, VariantStatus.APPROVED),
            (VariantStatus.REJECTED, VariantStatus.PUBLISHED),
        ],
    )
    def test_illegal_transitions_are_refused(
        self, current: VariantStatus, target: VariantStatus
    ) -> None:
        assert not can_transition(current, target)
        with pytest.raises(IllegalTransition):
            assert_transition(current, target)

    def test_no_state_transitions_to_itself(self) -> None:
        for state in VariantStatus:
            assert not can_transition(state, state)

    def test_the_table_is_exhaustive_over_every_pair(self) -> None:
        """Every pair is either explicitly legal or explicitly refused."""
        for current, target in itertools.product(VariantStatus, repeat=2):
            allowed = can_transition(current, target)
            if not allowed:
                with pytest.raises(IllegalTransition):
                    assert_transition(current, target)

    def test_approval_is_never_reachable_without_a_human_step(self) -> None:
        """GENERATED cannot become APPROVED in one move, by any path."""
        assert VariantStatus.APPROVED not in LEGAL_TRANSITIONS[VariantStatus.GENERATED]
        assert VariantStatus.PUBLISHED not in LEGAL_TRANSITIONS[VariantStatus.GENERATED]


class TestTransitionSideEffects:
    def test_approval_records_who_and_when(self, db: Session) -> None:
        from tests.factories import make_family as make_family_row
        from tests.factories import make_quiz as make_quiz_row
        from tests.factories import make_user, make_variant

        user = make_user(db)
        family = make_family_row(db, make_quiz_row(db, user))
        row = make_variant(db, family, validation_status=VariantStatus.VALIDATED)
        transition(row, VariantStatus.APPROVED, actor=user)
        assert row.approved_at is not None
        assert row.approved_by_id == user.id

    def test_rejection_records_the_reason_and_clears_approval(self, db: Session) -> None:
        from tests.factories import make_family as make_family_row
        from tests.factories import make_quiz as make_quiz_row
        from tests.factories import make_user, make_variant

        user = make_user(db)
        family = make_family_row(db, make_quiz_row(db, user))
        row = make_variant(db, family, validation_status=VariantStatus.APPROVED)
        transition(row, VariantStatus.REJECTED, actor=user, reason="ambiguous wording")
        assert row.rejection_reason == "ambiguous wording"
        assert row.approved_at is None
        assert row.approved_by_id is None


class TestApprovalAPI:
    def test_approve_then_the_variant_becomes_assignable(
        self, client: TestClient, teacher_a: dict[str, str], variant: dict
    ) -> None:
        response = client.post(f"/api/variants/{variant['id']}/approve", headers=teacher_a)
        assert response.status_code == 200
        assert response.json()["validation_status"] == "APPROVED"
        assert response.json()["is_assignable"] is True

    def test_approving_twice_is_refused(
        self, client: TestClient, teacher_a: dict[str, str], variant: dict
    ) -> None:
        client.post(f"/api/variants/{variant['id']}/approve", headers=teacher_a)
        second = client.post(f"/api/variants/{variant['id']}/approve", headers=teacher_a)
        assert second.status_code == 409
        assert second.json()["error"]["code"] == "ILLEGAL_VARIANT_TRANSITION"

    def test_rejection_makes_a_variant_unassignable(
        self, client: TestClient, teacher_a: dict[str, str], variant: dict
    ) -> None:
        response = client.post(
            f"/api/variants/{variant['id']}/reject", headers=teacher_a, json={"reason": "wrong"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["validation_status"] == "REJECTED"
        assert body["is_assignable"] is False
        assert body["rejection_reason"] == "wrong"

    def test_a_rejected_variant_can_be_reinstated_for_review(
        self, client: TestClient, teacher_a: dict[str, str], variant: dict
    ) -> None:
        client.post(f"/api/variants/{variant['id']}/reject", headers=teacher_a, json={})
        response = client.post(f"/api/variants/{variant['id']}/reinstate", headers=teacher_a)
        assert response.status_code == 200
        assert response.json()["validation_status"] == "VALIDATED"
        assert response.json()["rejection_reason"] is None

    def test_a_rejected_variant_cannot_be_approved_directly(
        self, client: TestClient, teacher_a: dict[str, str], variant: dict
    ) -> None:
        client.post(f"/api/variants/{variant['id']}/reject", headers=teacher_a, json={})
        response = client.post(f"/api/variants/{variant['id']}/approve", headers=teacher_a)
        assert response.status_code == 409

    def test_publishing_promotes_approved_variants(
        self, client: TestClient, teacher_a: dict[str, str], variant: dict
    ) -> None:
        client.post(f"/api/variants/{variant['id']}/approve", headers=teacher_a)
        client.post(f"/api/quizzes/{variant['_quiz_id']}/publish", headers=teacher_a)
        refreshed = client.get(f"/api/variants/{variant['id']}", headers=teacher_a).json()
        assert refreshed["validation_status"] == "PUBLISHED"
        assert refreshed["is_assignable"] is True

    def test_a_published_variant_can_still_be_withdrawn(
        self, client: TestClient, teacher_a: dict[str, str], variant: dict
    ) -> None:
        """A teacher who spots a bad question must be able to pull it."""
        client.post(f"/api/variants/{variant['id']}/approve", headers=teacher_a)
        client.post(f"/api/quizzes/{variant['_quiz_id']}/publish", headers=teacher_a)
        response = client.post(
            f"/api/variants/{variant['id']}/reject", headers=teacher_a, json={"reason": "bad"}
        )
        assert response.status_code == 200
        assert response.json()["is_assignable"] is False


class TestImmutabilityAndVersioning:
    def test_an_unreviewed_variant_is_edited_in_place(
        self, client: TestClient, teacher_a: dict[str, str], variant: dict
    ) -> None:
        """Nothing depends on it yet, so there is nothing to protect."""
        response = client.patch(
            f"/api/variants/{variant['id']}",
            headers=teacher_a,
            json={"question_text": "A completely rewritten question about complexity?"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["id"] == variant["id"], "same row"
        assert body["version"] == 1

    def test_editing_an_approved_variant_creates_a_new_version(
        self, client: TestClient, teacher_a: dict[str, str], variant: dict
    ) -> None:
        client.post(f"/api/variants/{variant['id']}/approve", headers=teacher_a)
        response = client.patch(
            f"/api/variants/{variant['id']}",
            headers=teacher_a,
            json={"question_text": "An edited question about search complexity entirely?"},
        )
        assert response.status_code == 200
        replacement = response.json()
        assert replacement["id"] != variant["id"], "a new row, not a rewrite"
        assert replacement["version"] == 2
        assert replacement["validation_status"] == "VALIDATED", "needs approving on its own merits"

    def test_the_original_survives_unchanged_after_being_superseded(
        self, client: TestClient, teacher_a: dict[str, str], variant: dict
    ) -> None:
        client.post(f"/api/variants/{variant['id']}/approve", headers=teacher_a)
        original_text = client.get(
            f"/api/variants/{variant['id']}", headers=teacher_a
        ).json()["question_text"]

        client.patch(
            f"/api/variants/{variant['id']}",
            headers=teacher_a,
            json={"question_text": "An edited question about search complexity entirely?"},
        )

        original = client.get(f"/api/variants/{variant['id']}", headers=teacher_a).json()
        assert original["question_text"] == original_text, "history must not change"
        assert original["superseded_by_id"] is not None

    def test_a_superseded_variant_is_no_longer_assignable(
        self, client: TestClient, teacher_a: dict[str, str], variant: dict
    ) -> None:
        """New papers must draw the newer version instead."""
        client.post(f"/api/variants/{variant['id']}/approve", headers=teacher_a)
        client.patch(
            f"/api/variants/{variant['id']}",
            headers=teacher_a,
            json={"question_text": "An edited question about search complexity entirely?"},
        )
        original = client.get(f"/api/variants/{variant['id']}", headers=teacher_a).json()
        assert original["validation_status"] == "APPROVED"
        assert original["is_assignable"] is False

    def test_editing_a_published_variant_supersedes_it(
        self, client: TestClient, teacher_a: dict[str, str], variant: dict
    ) -> None:
        client.post(f"/api/variants/{variant['id']}/approve", headers=teacher_a)
        client.post(f"/api/quizzes/{variant['_quiz_id']}/publish", headers=teacher_a)
        client.post(f"/api/quizzes/{variant['_quiz_id']}/unpublish", headers=teacher_a)

        response = client.patch(
            f"/api/variants/{variant['id']}",
            headers=teacher_a,
            json={"question_text": "A revised published question about complexity?"},
        )
        assert response.status_code == 200
        assert response.json()["id"] != variant["id"]
        assert response.json()["version"] == 2

    def test_an_edit_that_changes_nothing_is_refused(
        self, client: TestClient, teacher_a: dict[str, str], variant: dict
    ) -> None:
        client.post(f"/api/variants/{variant['id']}/approve", headers=teacher_a)
        response = client.patch(
            f"/api/variants/{variant['id']}",
            headers=teacher_a,
            json={"question_text": variant["question_text"]},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "NO_CHANGE"

    def test_an_edit_duplicating_a_sibling_is_refused(
        self, client: TestClient, teacher_a: dict[str, str], teacher_b: dict[str, str]
    ) -> None:
        quiz = make_quiz(client, teacher_a)
        family = make_family(client, teacher_a, quiz["id"])
        body = client.post(
            f"/api/questions/{family['id']}/generate-variants",
            headers=teacher_a,
            json={"count": 2},
        ).json()
        first, second = body["variants"][0], body["variants"][1]

        client.post(f"/api/variants/{first['id']}/approve", headers=teacher_a)
        response = client.patch(
            f"/api/variants/{first['id']}",
            headers=teacher_a,
            json={
                "question_text": second["question_text"],
                "options_json": second["options_json"],
            },
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "DUPLICATE_VARIANT"

    def test_a_published_variant_cannot_be_deleted(
        self, client: TestClient, teacher_a: dict[str, str], variant: dict
    ) -> None:
        client.post(f"/api/variants/{variant['id']}/approve", headers=teacher_a)
        client.post(f"/api/quizzes/{variant['_quiz_id']}/publish", headers=teacher_a)
        response = client.delete(f"/api/variants/{variant['id']}", headers=teacher_a)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "VARIANT_PUBLISHED"

    def test_an_unpublished_variant_can_be_deleted(
        self, client: TestClient, teacher_a: dict[str, str], variant: dict
    ) -> None:
        assert client.delete(f"/api/variants/{variant['id']}", headers=teacher_a).status_code == 204
        assert client.get(f"/api/variants/{variant['id']}", headers=teacher_a).status_code == 404


class TestVariantInUseByAnAttempt:
    """The strongest form of the rule, with a real attempt row referencing it."""

    def test_a_variant_served_to_a_student_cannot_be_edited_in_place(
        self, db: Session
    ) -> None:
        from app.services.variant_service import edit_variant
        from tests.factories import make_attempt, make_family, make_paper, make_quiz, make_user, make_variant

        teacher = make_user(db)
        quiz = make_quiz(db, teacher)
        family = make_family(db, quiz)
        row = make_variant(db, family, validation_status=VariantStatus.VALIDATED)
        attempt, _ = make_attempt(db, quiz)
        make_paper(db, attempt, [row])
        db.refresh(row)

        original_text = row.question_text
        replacement = edit_variant(
            db, row, {"question_text": "Rewritten after a student already saw it"}, teacher
        )

        assert replacement.id != row.id, "an in-use variant must be superseded, not rewritten"
        db.refresh(row)
        assert row.question_text == original_text, "the student's question must not change"

    def test_a_variant_served_to_a_student_cannot_be_deleted(self, db: Session) -> None:
        from app.core.errors import Conflict
        from app.services.variant_service import delete_variant
        from tests.factories import make_attempt, make_family, make_paper, make_quiz, make_user, make_variant

        teacher = make_user(db)
        quiz = make_quiz(db, teacher)
        family = make_family(db, quiz)
        row = make_variant(db, family, validation_status=VariantStatus.VALIDATED)
        attempt, _ = make_attempt(db, quiz)
        make_paper(db, attempt, [row])
        db.refresh(row)

        with pytest.raises(Conflict) as caught:
            delete_variant(db, row)
        assert caught.value.code == "VARIANT_IN_USE"
