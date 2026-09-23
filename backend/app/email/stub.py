"""Offline email provider for tests and local development.

Records what would have been sent so a test can assert on it, and can be told
to fail in each of the ways a real provider fails. Every automated test uses
this: no test run ever sends a real email or spends money.
"""

from __future__ import annotations

from app.email.base import (
    EmailMessage,
    EmailProvider,
    EmailProviderError,
    EmailResult,
)

STUB_MESSAGE_ID = "stub-message-id"


class StubEmailProvider(EmailProvider):
    name = "stub"

    def __init__(self, *, fail_with: EmailProviderError | None = None) -> None:
        self.fail_with = fail_with
        self.sent: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> EmailResult:
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append(message)
        return EmailResult(
            message_id=f"{STUB_MESSAGE_ID}-{len(self.sent)}", provider=self.name
        )


__all__ = ["STUB_MESSAGE_ID", "StubEmailProvider"]
