"""AI provider abstraction.

Nothing outside ``app/ai/`` imports a provider SDK or knows which model is in
use. Services depend on :class:`AIProvider` and on the plain dataclasses below,
so swapping Gemini for another provider is one new module plus a config value.

The division of labour is the rule the whole pipeline is built on:

    The provider generates language and structure.
    The backend decides what is true and what is allowed.

A provider returns *raw* candidates. It never decides whether a variant is
valid, whether it duplicates another, or what a numeric answer is.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.db.models import QuestionType


class AIProviderError(Exception):
    """Base class for provider failures.

    Every failure mode is a subclass so callers can distinguish "try again" from
    "this will never work". A provider outage must never corrupt a quiz, so
    these are caught at the service boundary and turned into a recorded
    generation failure rather than a 500.
    """

    retryable: bool = False

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class AITimeout(AIProviderError):
    retryable = True


class AIRateLimited(AIProviderError):
    """HTTP 429. Retryable, but only after a delay the provider chooses."""

    retryable = True

    def __init__(self, message: str, *, retry_after: float | None = None, detail: str | None = None):
        super().__init__(message, detail=detail)
        self.retry_after = retry_after


class AITransportError(AIProviderError):
    """Network failure or a 5xx from the provider."""

    retryable = True


class AIRequestRejected(AIProviderError):
    """4xx other than 429: bad key, bad request, content filter.

    Not retryable — the same request will fail the same way.
    """

    retryable = False


class AIMalformedResponse(AIProviderError):
    """The provider answered, but not with the JSON contract it was given.

    Deliberately not retryable at the transport layer: a schema failure is
    never retried into acceptance. The teacher can ask for regeneration.
    """

    retryable = False


@dataclass(frozen=True)
class VariantGenerationRequest:
    """What the caller wants generated.

    Carries the pedagogical context (topic, difficulty, learning objective) so
    the eventual assignment engine can select on the same axes the teacher
    authored against.
    """

    original_question: str
    question_type: QuestionType
    count: int
    topic: str | None = None
    difficulty: str | None = None
    learning_objective: str | None = None
    option_count: int = 4
    # Existing variant texts, so the prompt can ask for something different.
    # This is a hint to the model, never the duplicate check itself.
    avoid_texts: tuple[str, ...] = ()


@dataclass(frozen=True)
class RawVariant:
    """One unvalidated candidate exactly as the provider returned it.

    Every field is ``Any`` on purpose: this is untrusted input, and pretending
    otherwise would push the type errors into validation instead of catching
    them there.
    """

    question_text: Any = None
    options: Any = None
    correct_option: Any = None
    explanation: Any = None
    topic: Any = None
    difficulty: Any = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RawVariantBatch:
    """Provider output plus the provenance needed to audit it later."""

    variants: list[RawVariant]
    model: str
    prompt_version: str
    raw_text: str | None = None


class AIProvider(ABC):
    """Interface every provider implements."""

    name: str = "abstract"

    @abstractmethod
    async def generate_question_variants(
        self, request: VariantGenerationRequest
    ) -> RawVariantBatch:
        """Produce up to ``request.count`` candidate variants.

        Raises an :class:`AIProviderError` subclass on failure. Returning fewer
        candidates than asked for is a normal outcome, not an error — the
        service records it as partial generation.
        """

    async def aclose(self) -> None:
        """Release any transport resources. Safe to call more than once."""


__all__ = [
    "AIMalformedResponse",
    "AIProvider",
    "AIProviderError",
    "AIRateLimited",
    "AIRequestRejected",
    "AITimeout",
    "AITransportError",
    "RawVariant",
    "RawVariantBatch",
    "VariantGenerationRequest",
]
