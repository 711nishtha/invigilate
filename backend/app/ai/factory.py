"""Provider selection.

The one place that maps configuration to an implementation. Services ask for
``get_ai_provider()`` and receive an :class:`AIProvider`; they never name
Gemini.
"""

from __future__ import annotations

from app.ai.base import AIProvider
from app.core.config import settings

_override: AIProvider | None = None


def set_ai_provider(provider: AIProvider | None) -> None:
    """Install a provider for the process.

    Used by tests to inject a stub, and available as an escape hatch if a
    deployment needs to force a provider without redeploying config.
    """
    global _override
    _override = provider


def get_ai_provider() -> AIProvider:
    if _override is not None:
        return _override

    if settings.ai_provider == "stub":
        from app.ai.stub import StubProvider

        return StubProvider()

    from app.ai.gemini import GeminiProvider

    return GeminiProvider()


__all__ = ["get_ai_provider", "set_ai_provider"]
