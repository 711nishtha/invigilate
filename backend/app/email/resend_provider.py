"""Resend implementation of :class:`EmailProvider`.

The only module that knows Resend exists. Speaks the REST API over httpx rather
than pulling in an SDK, which keeps the dependency surface small and the
failure modes explicit.
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.email.base import (
    EmailMessage,
    EmailProvider,
    EmailRateLimited,
    EmailRejected,
    EmailResult,
    EmailTimeout,
    EmailTransportError,
)

logger = get_logger("securequiz.email.resend")

_API_URL = "https://api.resend.com/emails"
_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = (0.5, 2.0)


class ResendEmailProvider(EmailProvider):
    name = "resend"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        sender: str | None = None,
        timeout: float = 20.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else settings.resend_api_key
        self.sender = sender or settings.email_from
        self.timeout = timeout
        # Injectable so tests exercise the real request/response handling
        # against a mock transport instead of the network.
        self._client = client
        self._owns_client = client is None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def send(self, message: EmailMessage) -> EmailResult:
        if not self.api_key:
            raise EmailRejected("RESEND_API_KEY is not configured on this server.")

        payload: dict[str, Any] = {
            "from": self.sender,
            "to": [message.to],
            "subject": message.subject,
            "html": message.html,
        }
        if message.text:
            payload["text"] = message.text
        if message.attachments:
            payload["attachments"] = [
                {
                    "filename": attachment.filename,
                    "content": base64.b64encode(attachment.content).decode("ascii"),
                }
                for attachment in message.attachments
            ]

        body = await self._post_with_retries(payload)
        message_id = str(body.get("id") or "")
        logger.info(
            "Resend accepted a message",
            extra={
                "provider": self.name,
                "message_id": message_id,
                "attachments": len(message.attachments),
                # Recipient is the teacher who owns the quiz; recording it is
                # what makes a "did it send?" question answerable.
                "recipient_domain": message.to.rsplit("@", 1)[-1],
            },
        )
        return EmailResult(message_id=message_id, provider=self.name)

    async def _post_with_retries(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST with bounded retries on transient failures only."""
        client = await self._get_client()
        last_error: Exception | None = None

        for attempt in range(_MAX_ATTEMPTS):
            try:
                response = await client.post(
                    _API_URL,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    timeout=self.timeout,
                )
            except httpx.TimeoutException as exc:
                last_error = EmailTimeout(
                    f"Resend did not respond within {self.timeout}s.", detail=str(exc)
                )
            except httpx.HTTPError as exc:
                last_error = EmailTransportError("Could not reach Resend.", detail=str(exc))
            else:
                if response.status_code == 429:
                    last_error = EmailRateLimited(
                        "Resend rate limit reached.",
                        retry_after=_retry_after(response),
                        detail=_safe_detail(response),
                    )
                elif response.status_code >= 500:
                    last_error = EmailTransportError(
                        f"Resend returned {response.status_code}.", detail=_safe_detail(response)
                    )
                elif response.status_code >= 400:
                    # A bad key or an unverified sender will not fix itself.
                    raise EmailRejected(
                        f"Resend rejected the message ({response.status_code}).",
                        detail=_safe_detail(response),
                    )
                else:
                    return response.json() if response.content else {}

            if attempt < _MAX_ATTEMPTS - 1:
                delay = _BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS) - 1)]
                if isinstance(last_error, EmailRateLimited) and last_error.retry_after:
                    delay = min(last_error.retry_after, 10.0)
                logger.warning(
                    "Retrying Resend request",
                    extra={"attempt": attempt + 1, "delay_seconds": delay},
                )
                await asyncio.sleep(delay)

        assert last_error is not None
        raise last_error


def _retry_after(response: httpx.Response) -> float | None:
    header = response.headers.get("Retry-After")
    if not header:
        return None
    try:
        return float(header)
    except ValueError:
        return None


def _safe_detail(response: httpx.Response) -> str:
    """A short, non-sensitive slice of an error body.

    Truncated because a provider error can echo the request back, and the
    request contains the recipient and the message.
    """
    try:
        payload = response.json()
        message = payload.get("message") or payload.get("error") or ""
    except (ValueError, AttributeError):
        message = response.text
    return str(message)[:300]


__all__ = ["ResendEmailProvider"]
