"""Email provider abstraction.

Nothing outside ``app/email/`` knows Resend exists. Services depend on
:class:`EmailProvider` and the dataclasses below, so swapping provider is one
new module plus a config value.

The failure taxonomy mirrors ``app.ai.base``: callers need to tell "try again"
from "this will never work", because a result email that cannot be delivered
must never put the exam result itself at risk.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class EmailProviderError(Exception):
    """Base class for delivery failures."""

    retryable: bool = False

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class EmailTimeout(EmailProviderError):
    retryable = True


class EmailRateLimited(EmailProviderError):
    retryable = True

    def __init__(
        self, message: str, *, retry_after: float | None = None, detail: str | None = None
    ) -> None:
        super().__init__(message, detail=detail)
        self.retry_after = retry_after


class EmailTransportError(EmailProviderError):
    """Network failure or a 5xx from the provider."""

    retryable = True


class EmailRejected(EmailProviderError):
    """4xx other than 429: bad key, unverified sender, invalid recipient.

    Not retryable — the same request will fail the same way.
    """

    retryable = False


@dataclass(frozen=True)
class EmailAttachment:
    filename: str
    content: bytes
    content_type: str = "application/octet-stream"


@dataclass(frozen=True)
class EmailMessage:
    to: str
    subject: str
    html: str
    text: str | None = None
    attachments: tuple[EmailAttachment, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class EmailResult:
    """Provider acknowledgement. ``message_id`` is what a support ticket quotes."""

    message_id: str
    provider: str


class EmailProvider(ABC):
    name: str = "abstract"

    @abstractmethod
    async def send(self, message: EmailMessage) -> EmailResult:
        """Deliver one message, or raise an :class:`EmailProviderError`."""

    async def aclose(self) -> None:
        """Release transport resources. Safe to call more than once."""


__all__ = [
    "EmailAttachment",
    "EmailMessage",
    "EmailProvider",
    "EmailProviderError",
    "EmailRateLimited",
    "EmailRejected",
    "EmailResult",
    "EmailTimeout",
    "EmailTransportError",
]
