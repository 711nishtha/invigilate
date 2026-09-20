"""Offline provider for tests and local development.

Deterministic, free and instant. Every test in the suite runs against this, so
no test run can spend money or depend on a third party being up.

It can also be told to fail in each of the ways a real provider fails, which is
how the failure-handling paths are tested without mocking internals.
"""

from __future__ import annotations

from app.ai.base import (
    AIProvider,
    AIProviderError,
    RawVariant,
    RawVariantBatch,
    VariantGenerationRequest,
)
from app.db.models import QuestionType

STUB_MODEL = "stub-model-v1"
STUB_PROMPT_VERSION = "stub-v1"


class StubProvider(AIProvider):
    """Returns predictable, schema-correct variants.

    Parameters let a test bend the output into any shape the validator has to
    cope with, so the validation tests exercise the real pipeline rather than
    hand-built fixtures.
    """

    name = "stub"

    def __init__(
        self,
        *,
        fail_with: AIProviderError | None = None,
        variants: list[RawVariant] | None = None,
        return_count: int | None = None,
    ) -> None:
        self.fail_with = fail_with
        self.variants = variants
        self.return_count = return_count
        self.calls: list[VariantGenerationRequest] = []

    async def generate_question_variants(
        self, request: VariantGenerationRequest
    ) -> RawVariantBatch:
        self.calls.append(request)

        if self.fail_with is not None:
            raise self.fail_with

        if self.variants is not None:
            candidates = list(self.variants)
        else:
            count = self.return_count if self.return_count is not None else request.count
            candidates = [_build(request, index) for index in range(count)]

        return RawVariantBatch(
            variants=candidates,
            model=STUB_MODEL,
            prompt_version=STUB_PROMPT_VERSION,
            raw_text=None,
        )


def _build(request: VariantGenerationRequest, index: int) -> RawVariant:
    if request.question_type is QuestionType.TRUE_FALSE:
        return RawVariant(
            question_text=f"Statement {index + 1}: binary search needs a sorted array.",
            options=["True", "False"],
            correct_option=index % 2,
            explanation=f"Explanation for statement {index + 1}, which is long enough.",
            topic=request.topic or "Algorithms",
            difficulty=request.difficulty or "medium",
        )

    # Distinct option text per variant so the deduplicator sees genuinely
    # different content rather than rejecting the fixtures as duplicates.
    return RawVariant(
        question_text=f"Variant {index + 1}: what is the complexity of binary search?",
        options=[
            f"O(1) constant time, case {index + 1}",
            f"O(log n) logarithmic time, case {index + 1}",
            f"O(n) linear time, case {index + 1}",
            f"O(n log n) linearithmic time, case {index + 1}",
        ],
        correct_option=1,
        explanation=f"Halving the search space each step gives O(log n); case {index + 1}.",
        topic=request.topic or "Algorithms",
        difficulty=request.difficulty or "medium",
    )


__all__ = ["STUB_MODEL", "STUB_PROMPT_VERSION", "StubProvider"]
