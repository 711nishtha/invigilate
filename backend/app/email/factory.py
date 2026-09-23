"""Email provider selection.

The one place that maps configuration to an implementation. Services ask for
``get_email_provider()``; they never name Resend.
"""

from __future__ import annotations

from app.core.config import settings
from app.email.base import EmailProvider

_override: EmailProvider | None = None


def set_email_provider(provider: EmailProvider | None) -> None:
    """Install a provider for the process. Used by tests to inject a stub."""
    global _override
    _override = provider


def get_email_provider() -> EmailProvider:
    if _override is not None:
        return _override

    if settings.email_provider == "stub":
        from app.email.stub import StubEmailProvider

        return StubEmailProvider()

    from app.email.resend_provider import ResendEmailProvider

    return ResendEmailProvider()


__all__ = ["get_email_provider", "set_email_provider"]
