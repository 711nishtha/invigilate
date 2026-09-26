"""Generation orchestration: provider failures, partial results, numeric determinism.

A Gemini outage must not corrupt a quiz, block a published one, or surface as a
500. These tests drive the real service with a stub provider configured to fail
in each of the ways a real provider fails.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.ai.base import (
    AIMalformedResponse,
    AIRateLimited,
    AIRequestRejected,
    AITimeout,
    AITransportError,
    RawVariant,
)
from app.ai.factory import set_ai_provider
from app.ai.gemini import GeminiProvider, _parse_variants
from app.ai.stub import StubProvider
from app.db.models import QuestionType
from tests.test_ownership import make_family, make_quiz


@pytest.fixture
def family(client: TestClient, teacher_a: dict[str, str]) -> dict:
    quiz = make_quiz(client, teacher_a)
    return make_family(client, teacher_a, quiz["id"])


def generate(client: TestClient, headers: dict[str, str], family_id: str, **body) -> dict:  # type: ignore[no-untyped-def]
    response = client.post(
        f"/api/questions/{family_id}/generate-variants", headers=headers, json=body
    )
    assert response.status_code == 200, response.text
    return response.json()


class TestProviderFailures:
    @pytest.mark.parametrize(
        ("error", "expected_code"),
        [
            (AITimeout("timed out"), "AITimeout"),
            (AIRateLimited("429", retry_after=30), "AIRateLimited"),
            (AITransportError("network down"), "AITransportError"),
            (AIRequestRejected("bad key"), "AIRequestRejected"),
            (AIMalformedResponse("not json"), "AIMalformedResponse"),
        ],
    )
    def test_a_provider_failure_is_reported_not_raised(
        self, client: TestClient, teacher_a: dict[str, str], family: dict,
        error: Exception, expected_code: str
    ) -> None:
        set_ai_provider(StubProvider(fail_with=error))  # type: ignore[arg-type]
        body = generate(client, teacher_a, family["id"], count=3)
        assert body["error_code"] == expected_code
        assert body["accepted"] == 0
        assert body["variants"] == []

    def test_a_provider_failure_leaves_the_family_untouched(
        self, client: TestClient, teacher_a: dict[str, str], family: dict
    ) -> None:
        generate(client, teacher_a, family["id"], count=2)
        before = client.get(f"/api/questions/{family['id']}/variants", headers=teacher_a).json()

        set_ai_provider(StubProvider(fail_with=AITimeout("down")))
        generate(client, teacher_a, family["id"], count=2)

        after = client.get(f"/api/questions/{family['id']}/variants", headers=teacher_a).json()
        assert after == before

    def test_an_outage_does_not_affect_an_already_published_quiz(
        self, client: TestClient, teacher_a: dict[str, str]
    ) -> None:
        """The exam keeps working: generation is a preparation-time activity."""
        quiz = make_quiz(client, teacher_a)
        fam = make_family(client, teacher_a, quiz["id"])
        variant = generate(client, teacher_a, fam["id"], count=1)["variants"][0]
        client.post(f"/api/variants/{variant['id']}/approve", headers=teacher_a)
        published = client.post(f"/api/quizzes/{quiz['id']}/publish", headers=teacher_a)
        assert published.status_code == 200

        set_ai_provider(StubProvider(fail_with=AITransportError("gemini is down")))
        detail = client.get(f"/api/quizzes/{quiz['id']}", headers=teacher_a).json()
        assert detail["status"] == "PUBLISHED"
        assert detail["public_code"]
        assert detail["readiness"]["publishable"] is True

    def test_rate_limit_error_carries_retry_after(self) -> None:
        error = AIRateLimited("slow down", retry_after=42)
        assert error.retry_after == 42
        assert error.retryable is True

    def test_only_transient_errors_are_marked_retryable(self) -> None:
        assert AITimeout("x").retryable is True
        assert AITransportError("x").retryable is True
        assert AIRateLimited("x").retryable is True
        # A schema failure is never retried into acceptance.
        assert AIMalformedResponse("x").retryable is False
        assert AIRequestRejected("x").retryable is False


class TestPartialGeneration:
    def test_a_partly_invalid_batch_stores_the_valid_ones(
        self, client: TestClient, teacher_a: dict[str, str], family: dict
    ) -> None:
        set_ai_provider(
            StubProvider(
                variants=[
                    RawVariant(
                        question_text="A perfectly good question about binary search?",
                        options=["O(1)", "O(log n)", "O(n)", "O(n log n)"],
                        correct_option=1,
                        explanation="Halving the search space gives logarithmic time.",
                        topic="Algorithms",
                        difficulty="medium",
                        raw={"ok": True},
                    ),
                    RawVariant(question_text="", options=[], correct_option=None, raw={"bad": True}),
                ]
            )
        )
        body = generate(client, teacher_a, family["id"], count=2)
        assert body["accepted"] == 1
        assert len(body["rejected"]) == 1
        assert body["rejected"][0]["reasons"]

    def test_a_provider_returning_fewer_than_asked_is_not_an_error(
        self, client: TestClient, teacher_a: dict[str, str], family: dict
    ) -> None:
        set_ai_provider(StubProvider(return_count=2))
        body = generate(client, teacher_a, family["id"], count=5)
        assert body["accepted"] == 2
        assert body["error_code"] is None

    def test_regenerating_skips_duplicates_of_what_is_stored(
        self, client: TestClient, teacher_a: dict[str, str], family: dict
    ) -> None:
        first = generate(client, teacher_a, family["id"], count=3)
        assert first["accepted"] == 3
        second = generate(client, teacher_a, family["id"], count=3)
        assert second["accepted"] == 0
        assert len(second["rejected"]) == 3


class TestGeneratedVariantState:
    def test_generated_variants_await_human_approval(
        self, client: TestClient, teacher_a: dict[str, str], family: dict
    ) -> None:
        """Passing validation is not consent to show a question to students."""
        body = generate(client, teacher_a, family["id"], count=3)
        assert all(v["validation_status"] == "VALIDATED" for v in body["variants"])
        assert all(v["is_assignable"] is False for v in body["variants"])

    def test_provenance_is_recorded(
        self, client: TestClient, teacher_a: dict[str, str], family: dict
    ) -> None:
        body = generate(client, teacher_a, family["id"], count=1)
        variant = body["variants"][0]
        assert variant["generation_status"] == "AI_GENERATED"
        assert variant["generation_model"]
        assert variant["generation_prompt_version"]

    def test_the_family_context_is_passed_to_the_provider(
        self, client: TestClient, teacher_a: dict[str, str], family: dict
    ) -> None:
        stub = StubProvider()
        set_ai_provider(stub)
        generate(client, teacher_a, family["id"], count=2)
        request = stub.calls[0]
        assert request.topic == "Algorithms"
        assert request.difficulty == "medium"
        assert request.question_type is QuestionType.MCQ
        assert request.count == 2


class TestGeminiResponseParsing:
    """The provider's own parsing, exercised without the network."""

    def test_plain_json_parses(self) -> None:
        variants = _parse_variants('{"variants": [{"question_text": "Q", "options": []}]}')
        assert len(variants) == 1

    def test_a_markdown_fence_is_stripped(self) -> None:
        """A fence is a transport artefact, not content repair."""
        variants = _parse_variants('```json\n{"variants": [{"question_text": "Q"}]}\n```')
        assert len(variants) == 1

    def test_invalid_json_is_rejected(self) -> None:
        with pytest.raises(AIMalformedResponse):
            _parse_variants("this is not json at all")

    def test_json_without_a_variants_array_is_rejected(self) -> None:
        with pytest.raises(AIMalformedResponse):
            _parse_variants('{"result": "ok"}')

    def test_prose_around_json_is_rejected_not_salvaged(self) -> None:
        with pytest.raises(AIMalformedResponse):
            _parse_variants('Sure! Here are your variants: {"variants": []}')

    def test_a_non_object_entry_becomes_a_rejectable_candidate(self) -> None:
        variants = _parse_variants('{"variants": ["just a string"]}')
        assert len(variants) == 1
        assert "invalid" in variants[0].raw


class TestGeminiTransport:
    """HTTP handling against a mock transport, so no network is touched."""

    async def _run(self, handler, **kwargs):  # type: ignore[no-untyped-def]
        from app.ai.base import VariantGenerationRequest

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            provider = GeminiProvider(api_key="test-key", client=client, **kwargs)
            return await provider.generate_question_variants(
                VariantGenerationRequest(
                    original_question="Q", question_type=QuestionType.MCQ, count=1
                )
            )

    @pytest.mark.anyio
    async def test_a_successful_response_is_parsed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {"content": {"parts": [{"text": '{"variants": [{"question_text": "Q"}]}'}]}}
                    ]
                },
            )

        batch = await self._run(handler)
        assert len(batch.variants) == 1
        assert batch.prompt_version

    @pytest.mark.anyio
    async def test_a_401_is_not_retried(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(401, json={"error": {"message": "invalid key"}})

        with pytest.raises(AIRequestRejected):
            await self._run(handler)
        assert calls["n"] == 1, "a bad key will not fix itself on a retry"

    @pytest.mark.anyio
    async def test_a_429_is_retried_then_surfaces(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(429, headers={"Retry-After": "0"}, json={})

        with pytest.raises(AIRateLimited):
            await self._run(handler)
        assert calls["n"] > 1, "a rate limit should be retried before giving up"

    @pytest.mark.anyio
    async def test_a_500_is_retried(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] < 2:
                return httpx.Response(503, json={})
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {"content": {"parts": [{"text": '{"variants": [{"question_text": "Q"}]}'}]}}
                    ]
                },
            )

        batch = await self._run(handler)
        assert calls["n"] == 2
        assert len(batch.variants) == 1

    @pytest.mark.anyio
    async def test_a_safety_block_is_a_malformed_response(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}})

        with pytest.raises(AIMalformedResponse):
            await self._run(handler)

    @pytest.mark.anyio
    async def test_the_api_key_is_sent_as_a_header_not_a_query_param(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["header"] = request.headers.get("x-goog-api-key", "")
            seen["url"] = str(request.url)
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {"content": {"parts": [{"text": '{"variants": [{"question_text": "Q"}]}'}]}}
                    ]
                },
            )

        await self._run(handler)
        assert seen["header"] == "test-key"
        # A key in the URL would land in access logs and proxy history.
        assert "test-key" not in seen["url"]

    @pytest.mark.anyio
    async def test_a_missing_api_key_fails_before_any_request(self) -> None:
        from app.ai.base import VariantGenerationRequest

        provider = GeminiProvider(api_key="")
        with pytest.raises(AIRequestRejected):
            await provider.generate_question_variants(
                VariantGenerationRequest(
                    original_question="Q", question_type=QuestionType.MCQ, count=1
                )
            )


class TestGenerationIsPreparationOnly:
    def test_generation_is_not_reachable_without_a_teacher_token(
        self, client: TestClient, family: dict
    ) -> None:
        response = client.post(
            f"/api/questions/{family['id']}/generate-variants", json={"count": 1}
        )
        assert response.status_code == 401

    def test_generation_is_only_reachable_from_teacher_routes(self) -> None:
        """Generation must never sit on a student's critical path.

        Checked structurally: every route that calls the generation service
        must also depend on CurrentTeacher.
        """
        import pathlib
        import re

        callers = []
        for path in pathlib.Path("app/api/routes").glob("*.py"):
            source = path.read_text(encoding="utf-8")
            if "generate_variants" not in source:
                continue
            for match in re.finditer(
                r"(?:async )?def (\w+)\((.*?)\) ->", source, re.DOTALL
            ):
                name, params = match.group(1), match.group(2)
                body_start = match.end()
                body = source[body_start : body_start + 800]
                if "variant_service.generate_variants" in body:
                    callers.append((path.name, name, "CurrentTeacher" in params))

        assert callers, "expected at least one generation route to exist"
        unguarded = [(f, n) for f, n, guarded in callers if not guarded]
        assert not unguarded, f"generation reachable without a teacher guard: {unguarded}"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class TestGenerationRateLimit:
    """Generation costs money, so it carries a tighter limit than the default."""

    def test_repeated_generation_is_eventually_rate_limited(
        self, client: TestClient, teacher_a: dict[str, str], family: dict, rate_limited
    ) -> None:
        statuses = []
        for _ in range(25):  # configured limit is 20/minute
            response = client.post(
                f"/api/questions/{family['id']}/generate-variants",
                headers=teacher_a,
                json={"count": 1},
            )
            statuses.append(response.status_code)
            if response.status_code == 429:
                break

        assert 429 in statuses, "generation must be rate limited"
        limited = client.post(
            f"/api/questions/{family['id']}/generate-variants",
            headers=teacher_a,
            json={"count": 1},
        )
        assert limited.status_code == 429
        assert limited.json()["error"]["code"] == "RATE_LIMITED"
        assert limited.headers.get("Retry-After")
