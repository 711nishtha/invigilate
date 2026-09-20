"""Publish eligibility, quiz lifecycle, and the selection prerequisites.

Phase 2 stops short of creating attempts. What it does guarantee is that a
publishable quiz can actually produce a valid paper, and that the selection
primitives Phase 4 will build on are deterministic and family-first.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.models import VariantStatus
from app.services.publish_rules import BlockerCode, evaluate_publish_readiness
from app.services.selection import (
    SelectionCriteria,
    build_pools,
    distinct_paper_estimate,
    filter_pools,
    plan_paper,
)
from tests.test_ownership import make_family, make_quiz


def approve_one(client: TestClient, headers: dict[str, str], family_id: str) -> dict:
    body = client.post(
        f"/api/questions/{family_id}/generate-variants", headers=headers, json={"count": 1}
    ).json()
    variant = body["variants"][0]
    client.post(f"/api/variants/{variant['id']}/approve", headers=headers)
    return variant


def blocker_codes(readiness: dict) -> set[str]:
    return {b["code"] for b in readiness["blockers"]}


class TestPublishBlockers:
    def test_an_empty_quiz_cannot_be_published(
        self, client: TestClient, teacher_a: dict[str, str]
    ) -> None:
        quiz = make_quiz(client, teacher_a)
        readiness = client.get(f"/api/quizzes/{quiz['id']}/readiness", headers=teacher_a).json()
        assert readiness["publishable"] is False
        assert BlockerCode.NO_QUESTION_FAMILIES.value in blocker_codes(readiness)

    def test_a_family_without_an_approved_variant_blocks_publication(
        self, client: TestClient, teacher_a: dict[str, str]
    ) -> None:
        quiz = make_quiz(client, teacher_a)
        family = make_family(client, teacher_a, quiz["id"])
        client.post(
            f"/api/questions/{family['id']}/generate-variants", headers=teacher_a, json={"count": 2}
        )
        readiness = client.get(f"/api/quizzes/{quiz['id']}/readiness", headers=teacher_a).json()
        assert readiness["publishable"] is False
        assert BlockerCode.FAMILY_WITHOUT_APPROVED_VARIANT.value in blocker_codes(readiness)

    def test_generated_variants_alone_do_not_make_a_quiz_publishable(
        self, client: TestClient, teacher_a: dict[str, str]
    ) -> None:
        """Validation is not approval. This is the core review guarantee."""
        quiz = make_quiz(client, teacher_a)
        family = make_family(client, teacher_a, quiz["id"])
        client.post(
            f"/api/questions/{family['id']}/generate-variants", headers=teacher_a, json={"count": 5}
        )
        response = client.post(f"/api/quizzes/{quiz['id']}/publish", headers=teacher_a)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "QUIZ_NOT_READY"

    def test_too_few_ready_families_blocks_publication(
        self, client: TestClient, teacher_a: dict[str, str]
    ) -> None:
        quiz = make_quiz(client, teacher_a, question_count=3)
        family = make_family(client, teacher_a, quiz["id"])
        approve_one(client, teacher_a, family["id"])
        readiness = client.get(f"/api/quizzes/{quiz['id']}/readiness", headers=teacher_a).json()
        assert BlockerCode.NOT_ENOUGH_FAMILIES.value in blocker_codes(readiness)

    def test_blockers_name_the_family_to_fix(
        self, client: TestClient, teacher_a: dict[str, str]
    ) -> None:
        quiz = make_quiz(client, teacher_a)
        family = make_family(client, teacher_a, quiz["id"])
        readiness = client.get(f"/api/quizzes/{quiz['id']}/readiness", headers=teacher_a).json()
        offending = [b for b in readiness["blockers"] if b["family_id"]]
        assert offending and offending[0]["family_id"] == family["id"]

    def test_a_rejected_variant_does_not_count_towards_readiness(
        self, client: TestClient, teacher_a: dict[str, str]
    ) -> None:
        quiz = make_quiz(client, teacher_a)
        family = make_family(client, teacher_a, quiz["id"])
        variant = approve_one(client, teacher_a, family["id"])
        client.post(f"/api/variants/{variant['id']}/reject", headers=teacher_a, json={})
        readiness = client.get(f"/api/quizzes/{quiz['id']}/readiness", headers=teacher_a).json()
        assert readiness["publishable"] is False
        assert BlockerCode.FAMILY_WITHOUT_APPROVED_VARIANT.value in blocker_codes(readiness)

    def test_a_short_answer_family_cannot_be_published(
        self, client: TestClient, teacher_a: dict[str, str]
    ) -> None:
        """It cannot be auto-graded, so it cannot carry an exam yet."""
        quiz = make_quiz(client, teacher_a)
        response = client.post(
            f"/api/quizzes/{quiz['id']}/questions",
            headers=teacher_a,
            json={"original_question": "Explain why binary search needs sorted input.",
                  "question_type": "SHORT_ANSWER"},
        )
        assert response.status_code == 201
        readiness = client.get(f"/api/quizzes/{quiz['id']}/readiness", headers=teacher_a).json()
        assert BlockerCode.UNSUPPORTED_QUESTION_TYPE.value in blocker_codes(readiness)

    def test_coverage_requirements_are_enforced_when_configured(
        self, client: TestClient, teacher_a: dict[str, str]
    ) -> None:
        quiz = make_quiz(
            client, teacher_a, coverage_config_json={"required_topics": ["Graph theory"]}
        )
        family = make_family(client, teacher_a, quiz["id"])  # topic is Algorithms
        approve_one(client, teacher_a, family["id"])
        readiness = client.get(f"/api/quizzes/{quiz['id']}/readiness", headers=teacher_a).json()
        assert BlockerCode.TOPIC_COVERAGE_UNMET.value in blocker_codes(readiness)

    def test_no_coverage_config_means_no_coverage_blockers(
        self, client: TestClient, teacher_a: dict[str, str]
    ) -> None:
        quiz = make_quiz(client, teacher_a)
        family = make_family(client, teacher_a, quiz["id"])
        approve_one(client, teacher_a, family["id"])
        readiness = client.get(f"/api/quizzes/{quiz['id']}/readiness", headers=teacher_a).json()
        assert readiness["publishable"] is True

    def test_an_unknown_coverage_key_is_refused_at_the_schema(
        self, client: TestClient, teacher_a: dict[str, str]
    ) -> None:
        """Otherwise a teacher believes a requirement is enforced when nothing reads it."""
        response = client.post(
            "/api/quizzes",
            headers=teacher_a,
            json={"title": "Q", "duration_seconds": 600, "question_count": 1,
                  "coverage_config_json": {"required_moon_phase": ["full"]}},
        )
        assert response.status_code == 422


class TestPublishLifecycle:
    @pytest.fixture
    def ready_quiz(self, client: TestClient, teacher_a: dict[str, str]) -> dict:
        quiz = make_quiz(client, teacher_a)
        family = make_family(client, teacher_a, quiz["id"])
        approve_one(client, teacher_a, family["id"])
        return quiz

    def test_publishing_assigns_a_public_code(
        self, client: TestClient, teacher_a: dict[str, str], ready_quiz: dict
    ) -> None:
        body = client.post(f"/api/quizzes/{ready_quiz['id']}/publish", headers=teacher_a).json()
        assert body["status"] == "PUBLISHED"
        assert body["public_code"] and len(body["public_code"]) == 6
        assert not set(body["public_code"]) & set("OI01L"), "ambiguous characters excluded"

    def test_publishing_twice_is_idempotent(
        self, client: TestClient, teacher_a: dict[str, str], ready_quiz: dict
    ) -> None:
        first = client.post(f"/api/quizzes/{ready_quiz['id']}/publish", headers=teacher_a).json()
        second = client.post(f"/api/quizzes/{ready_quiz['id']}/publish", headers=teacher_a).json()
        assert first["public_code"] == second["public_code"]
        assert first["published_at"] == second["published_at"]

    def test_exam_shape_cannot_change_while_published(
        self, client: TestClient, teacher_a: dict[str, str], ready_quiz: dict
    ) -> None:
        """Changing the duration underneath a live exam would corrupt it."""
        client.post(f"/api/quizzes/{ready_quiz['id']}/publish", headers=teacher_a)
        response = client.patch(
            f"/api/quizzes/{ready_quiz['id']}", headers=teacher_a, json={"duration_seconds": 60}
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "QUIZ_PUBLISHED_IMMUTABLE_FIELDS"
        assert "duration_seconds" in response.json()["error"]["details"]["fields"]

    def test_cosmetic_fields_can_still_change_while_published(
        self, client: TestClient, teacher_a: dict[str, str], ready_quiz: dict
    ) -> None:
        client.post(f"/api/quizzes/{ready_quiz['id']}/publish", headers=teacher_a)
        response = client.patch(
            f"/api/quizzes/{ready_quiz['id']}", headers=teacher_a, json={"description": "Updated"}
        )
        assert response.status_code == 200
        assert response.json()["description"] == "Updated"

    def test_questions_cannot_be_added_while_published(
        self, client: TestClient, teacher_a: dict[str, str], ready_quiz: dict
    ) -> None:
        client.post(f"/api/quizzes/{ready_quiz['id']}/publish", headers=teacher_a)
        response = client.post(
            f"/api/quizzes/{ready_quiz['id']}/questions",
            headers=teacher_a,
            json={"original_question": "A new question added mid-exam?", "question_type": "MCQ"},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "QUIZ_PUBLISHED_IMMUTABLE"

    def test_unpublishing_returns_a_quiz_to_draft(
        self, client: TestClient, teacher_a: dict[str, str], ready_quiz: dict
    ) -> None:
        client.post(f"/api/quizzes/{ready_quiz['id']}/publish", headers=teacher_a)
        response = client.post(f"/api/quizzes/{ready_quiz['id']}/unpublish", headers=teacher_a)
        assert response.status_code == 200
        assert response.json()["status"] == "DRAFT"
        assert response.json()["published_at"] is None

    def test_unpublishing_keeps_the_public_code_for_republishing(
        self, client: TestClient, teacher_a: dict[str, str], ready_quiz: dict
    ) -> None:
        code = client.post(
            f"/api/quizzes/{ready_quiz['id']}/publish", headers=teacher_a
        ).json()["public_code"]
        client.post(f"/api/quizzes/{ready_quiz['id']}/unpublish", headers=teacher_a)
        again = client.post(f"/api/quizzes/{ready_quiz['id']}/publish", headers=teacher_a).json()
        assert again["public_code"] == code

    def test_an_unpublished_draft_cannot_be_unpublished_again(
        self, client: TestClient, teacher_a: dict[str, str], ready_quiz: dict
    ) -> None:
        response = client.post(f"/api/quizzes/{ready_quiz['id']}/unpublish", headers=teacher_a)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "QUIZ_NOT_PUBLISHED"

    def test_a_quiz_with_attempts_cannot_be_unpublished(
        self, client: TestClient, teacher_a: dict[str, str], ready_quiz: dict, db: Session
    ) -> None:
        """Students have started; the exit is to close, not to yank it away."""
        from sqlalchemy import select

        from app.db.models import Quiz
        from tests.factories import make_attempt

        client.post(f"/api/quizzes/{ready_quiz['id']}/publish", headers=teacher_a)
        quiz_row = db.scalar(select(Quiz).where(Quiz.id == ready_quiz["id"]))
        make_attempt(db, quiz_row, email="student@example.edu")

        response = client.post(f"/api/quizzes/{ready_quiz['id']}/unpublish", headers=teacher_a)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "QUIZ_HAS_ATTEMPTS"

    def test_closing_stops_new_attempts(
        self, client: TestClient, teacher_a: dict[str, str], ready_quiz: dict
    ) -> None:
        client.post(f"/api/quizzes/{ready_quiz['id']}/publish", headers=teacher_a)
        response = client.post(f"/api/quizzes/{ready_quiz['id']}/close", headers=teacher_a)
        assert response.status_code == 200
        assert response.json()["status"] == "CLOSED"

    def test_a_closed_quiz_cannot_be_republished(
        self, client: TestClient, teacher_a: dict[str, str], ready_quiz: dict
    ) -> None:
        client.post(f"/api/quizzes/{ready_quiz['id']}/publish", headers=teacher_a)
        client.post(f"/api/quizzes/{ready_quiz['id']}/close", headers=teacher_a)
        response = client.post(f"/api/quizzes/{ready_quiz['id']}/publish", headers=teacher_a)
        assert response.status_code == 409


class TestSelectionPrerequisites:
    """Phase 4's assignment engine will call these. No attempts are created."""

    @pytest.fixture
    def pooled_quiz(self, db: Session):  # type: ignore[no-untyped-def]
        from tests.factories import make_family as make_family_row
        from tests.factories import make_quiz as make_quiz_row
        from tests.factories import make_user, make_variant

        teacher = make_user(db)
        quiz = make_quiz_row(db, teacher, question_count=3)
        for position in range(5):
            family = make_family_row(db, quiz, position=position, topic=f"Topic{position % 2}")
            for _ in range(4):
                make_variant(db, family, validation_status=VariantStatus.APPROVED)
        db.refresh(quiz)
        return quiz

    def test_only_assignable_variants_enter_the_pool(self, db: Session) -> None:
        from tests.factories import make_family as make_family_row
        from tests.factories import make_quiz as make_quiz_row
        from tests.factories import make_user, make_variant

        teacher = make_user(db)
        quiz = make_quiz_row(db, teacher)
        family = make_family_row(db, quiz)
        make_variant(db, family, validation_status=VariantStatus.APPROVED)
        make_variant(db, family, validation_status=VariantStatus.PUBLISHED)
        make_variant(db, family, validation_status=VariantStatus.VALIDATED)
        make_variant(db, family, validation_status=VariantStatus.GENERATED)
        make_variant(db, family, validation_status=VariantStatus.REJECTED)

        pools = build_pools(db, quiz)
        assert len(pools) == 1
        assert pools[0].size == 2, "only APPROVED and PUBLISHED are assignable"

    def test_superseded_variants_are_excluded(self, db: Session) -> None:
        from tests.factories import make_family as make_family_row
        from tests.factories import make_quiz as make_quiz_row
        from tests.factories import make_user, make_variant

        teacher = make_user(db)
        quiz = make_quiz_row(db, teacher)
        family = make_family_row(db, quiz)
        old = make_variant(db, family, validation_status=VariantStatus.APPROVED)
        new = make_variant(db, family, validation_status=VariantStatus.APPROVED)
        old.superseded_by_id = new.id
        db.commit()

        pools = build_pools(db, quiz)
        assert pools[0].variant_ids == (new.id,)

    def test_a_plan_is_reproducible_from_its_seed(self, db: Session, pooled_quiz) -> None:  # type: ignore[no-untyped-def]
        pools = build_pools(db, pooled_quiz)
        first = plan_paper(pools, seed=42, question_count=3)
        second = plan_paper(pools, seed=42, question_count=3)
        assert first == second, "an attempt must be re-derivable from its stored seed"

    def test_different_seeds_produce_different_papers(self, db: Session, pooled_quiz) -> None:  # type: ignore[no-untyped-def]
        pools = build_pools(db, pooled_quiz)
        papers = {tuple(plan_paper(pools, seed=seed, question_count=3)) for seed in range(40)}
        assert len(papers) > 1, "the same link must not give everyone the same paper"

    def test_a_paper_never_repeats_a_family(self, db: Session, pooled_quiz) -> None:  # type: ignore[no-untyped-def]
        """Row-level random selection would give one student two variants of one question."""
        pools = build_pools(db, pooled_quiz)
        for seed in range(30):
            plan = plan_paper(pools, seed=seed, question_count=3)
            families = [family_id for family_id, _ in plan]
            assert len(families) == len(set(families))

    def test_a_paper_has_the_configured_question_count(self, db: Session, pooled_quiz) -> None:  # type: ignore[no-untyped-def]
        pools = build_pools(db, pooled_quiz)
        assert len(plan_paper(pools, seed=1, question_count=3)) == 3

    def test_asking_for_more_questions_than_families_yields_all_families(
        self, db: Session, pooled_quiz
    ) -> None:  # type: ignore[no-untyped-def]
        pools = build_pools(db, pooled_quiz)
        assert len(plan_paper(pools, seed=1, question_count=99)) == len(pools)

    def test_every_chosen_variant_comes_from_its_family_pool(
        self, db: Session, pooled_quiz
    ) -> None:  # type: ignore[no-untyped-def]
        pools = build_pools(db, pooled_quiz)
        by_family = {pool.family_id: set(pool.variant_ids) for pool in pools}
        for seed in range(20):
            for family_id, variant_id in plan_paper(pools, seed=seed, question_count=3):
                assert variant_id in by_family[family_id]

    def test_pools_can_be_filtered_by_topic(self, db: Session, pooled_quiz) -> None:  # type: ignore[no-untyped-def]
        pools = build_pools(db, pooled_quiz)
        filtered = filter_pools(pools, SelectionCriteria(question_count=3, topics=("Topic0",)))
        assert filtered and all(pool.topic == "Topic0" for pool in filtered)

    def test_an_empty_filter_matches_everything(self, db: Session, pooled_quiz) -> None:  # type: ignore[no-untyped-def]
        pools = build_pools(db, pooled_quiz)
        assert filter_pools(pools, SelectionCriteria(question_count=3)) == pools

    def test_criteria_are_read_from_the_quiz(self, db: Session, pooled_quiz) -> None:  # type: ignore[no-untyped-def]
        pooled_quiz.coverage_config_json = {"required_topics": ["Topic1"]}
        db.commit()
        criteria = SelectionCriteria.for_quiz(pooled_quiz)
        assert criteria.topics == ("Topic1",)
        assert criteria.question_count == 3

    def test_distinct_paper_estimate(self, db: Session, pooled_quiz) -> None:  # type: ignore[no-untyped-def]
        pools = build_pools(db, pooled_quiz)
        assert distinct_paper_estimate(pools, 3) == 4 * 4 * 4

    def test_an_empty_pool_plans_nothing(self, db: Session) -> None:
        assert plan_paper([], seed=1, question_count=5) == []
        assert distinct_paper_estimate([], 5) == 0
