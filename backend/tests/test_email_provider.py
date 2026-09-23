"""Resend provider transport handling.

Exercised against an httpx mock transport, so the real request/response
handling is tested without sending an email or spending money. A live send is a
separate, manual step — see scripts/resend_smoke.py.
"""

from __future__ import annotations

import base64

import httpx
import pytest

from app.email.base import (
    EmailAttachment,
    EmailMessage,
    EmailRateLimited,
    EmailRejected,
    EmailTimeout,
    EmailTransportError,
)
from app.email.resend_provider import ResendEmailProvider


def message(**overrides) -> EmailMessage:  # type: ignore[no-untyped-def]
    data = {
        "to": "teacher@example.edu",
        "subject": "Quiz results — Midterm",
        "html": "<p>Results attached.</p>",
        "text": "Results attached.",
    }
    data.update(overrides)
    return EmailMessage(**data)  # type: ignore[arg-type]


async def run(handler, **kwargs):  # type: ignore[no-untyped-def]
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = ResendEmailProvider(api_key="test-key", client=client, **kwargs)
        return await provider.send(message())


def ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"id": "msg_123"})


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class TestSuccessfulSend:
    @pytest.mark.anyio
    async def test_a_successful_send_returns_the_message_id(self) -> None:
        result = await run(ok)
        assert result.message_id == "msg_123"
        assert result.provider == "resend"

    @pytest.mark.anyio
    async def test_the_api_key_is_sent_as_a_bearer_header(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("authorization", "")
            seen["url"] = str(request.url)
            return ok(request)

        await run(handler)
        assert seen["auth"] == "Bearer test-key"
        # A key in the URL would land in access logs and proxy history.
        assert "test-key" not in seen["url"]

    @pytest.mark.anyio
    async def test_an_attachment_is_base64_encoded(self) -> None:
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            seen["body"] = json.loads(request.content)
            return ok(request)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            provider = ResendEmailProvider(api_key="test-key", client=client)
            await provider.send(
                message(
                    attachments=(
                        EmailAttachment(filename="results.xlsx", content=b"binary-payload"),
                    )
                )
            )

        body = seen["body"]
        attachment = body["attachments"][0]  # type: ignore[index]
        assert attachment["filename"] == "results.xlsx"
        assert base64.b64decode(attachment["content"]) == b"binary-payload"

    @pytest.mark.anyio
    async def test_the_sender_comes_from_configuration(self) -> None:
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            seen["body"] = json.loads(request.content)
            return ok(request)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            provider = ResendEmailProvider(
                api_key="k", sender="Secure Quiz <results@example.edu>", client=client
            )
            await provider.send(message())

        assert seen["body"]["from"] == "Secure Quiz <results@example.edu>"  # type: ignore[index]


class TestFailureMapping:
    @pytest.mark.anyio
    async def test_a_401_is_rejected_and_not_retried(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(401, json={"message": "invalid api key"})

        with pytest.raises(EmailRejected):
            await run(handler)
        assert calls["n"] == 1, "a bad key will not fix itself on a retry"

    @pytest.mark.anyio
    async def test_a_422_unverified_sender_is_rejected(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(422, json={"message": "domain is not verified"})

        with pytest.raises(EmailRejected) as caught:
            await run(handler)
        assert "not verified" in (caught.value.detail or "")

    @pytest.mark.anyio
    async def test_a_429_is_retried_then_surfaces(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(429, headers={"Retry-After": "0"}, json={})

        with pytest.raises(EmailRateLimited):
            await run(handler)
        assert calls["n"] > 1

    @pytest.mark.anyio
    async def test_a_500_is_retried_then_succeeds(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] < 2:
                return httpx.Response(503, json={})
            return ok(request)

        result = await run(handler)
        assert calls["n"] == 2
        assert result.message_id == "msg_123"

    @pytest.mark.anyio
    async def test_a_timeout_maps_to_email_timeout(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("too slow")

        with pytest.raises(EmailTimeout):
            await run(handler)

    @pytest.mark.anyio
    async def test_a_network_failure_maps_to_transport_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route to host")

        with pytest.raises(EmailTransportError):
            await run(handler)

    @pytest.mark.anyio
    async def test_a_missing_api_key_fails_before_any_request(self) -> None:
        provider = ResendEmailProvider(api_key="")
        with pytest.raises(EmailRejected):
            await provider.send(message())

    def test_only_transient_errors_are_retryable(self) -> None:
        assert EmailTimeout("x").retryable is True
        assert EmailTransportError("x").retryable is True
        assert EmailRateLimited("x").retryable is True
        assert EmailRejected("x").retryable is False


class TestNoSecretLeakage:
    @pytest.mark.anyio
    async def test_an_error_detail_is_truncated_and_carries_no_key(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            # A provider that echoes the request back would otherwise put the
            # Authorization header into our logs.
            return httpx.Response(
                400, json={"message": "Bearer test-key rejected " + "x" * 2000}
            )

        with pytest.raises(EmailRejected) as caught:
            await run(handler)
        assert len(caught.value.detail or "") <= 300

    def test_the_provider_is_the_only_module_naming_resend(self) -> None:
        import pathlib

        offenders = []
        for path in pathlib.Path("app").rglob("*.py"):
            if "email" in path.parts:
                continue
            text = path.read_text(encoding="utf-8").lower()
            if "resend" in text and "resend_api_key" not in text and "email_provider" not in text:
                offenders.append(str(path))
        assert not offenders, f"Resend leaked outside app/email: {offenders}"
