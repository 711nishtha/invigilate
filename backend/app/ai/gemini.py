"""Gemini implementation of :class:`AIProvider`.

This is the only module in the codebase that knows Gemini exists. It speaks
the REST API over httpx rather than pulling in an SDK, which keeps the
dependency surface small and the failure modes explicit.

It converts transport and protocol problems into the typed errors in
``app.ai.base``. It performs no validation of question content: deciding
whether a variant is usable is the backend's job, in ``app.ai.validation``.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx

from app.ai.base import (
    AIMalformedResponse,
    AIProvider,
    AIRateLimited,
    AIRequestRejected,
    AITimeout,
    AITransportError,
    RawVariant,
    RawVariantBatch,
    VariantGenerationRequest,
)
from app.ai.prompts import build_variant_prompt
from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("securequiz.ai.gemini")

_API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
# Models occasionally wrap JSON in a markdown fence despite being told not to.
# Stripping a fence is a parsing concession, not content repair.
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)

_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = (0.5, 2.0)


class GeminiProvider(AIProvider):
    name = "gemini"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else settings.gemini_api_key
        self.model = model or settings.gemini_model
        self.timeout = timeout or settings.ai_request_timeout_seconds
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

    async def generate_question_variants(
        self, request: VariantGenerationRequest
    ) -> RawVariantBatch:
        if not self.api_key:
            raise AIRequestRejected("GEMINI_API_KEY is not configured on this server.")

        prompt, prompt_version = build_variant_prompt(request)
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                # Low temperature: variety comes from asking for several
                # variants, not from an unpredictable decoder.
                "temperature": 0.7,
                "topP": 0.95,
                "maxOutputTokens": 4096,
                # Ask the API itself to constrain the output to JSON where it
                # can. Validation still runs regardless.
                "responseMimeType": "application/json",
            },
        }

        raw_text = await self._post_with_retries(payload)
        variants = _parse_variants(raw_text)

        logger.info(
            "Gemini generation returned candidates",
            extra={
                "model": self.model,
                "prompt_version": prompt_version,
                "requested": request.count,
                "returned": len(variants),
            },
        )
        return RawVariantBatch(
            variants=variants,
            model=self.model,
            prompt_version=prompt_version,
            raw_text=raw_text,
        )

    async def _post_with_retries(self, payload: dict[str, Any]) -> str:
        """POST with bounded retries on transient failures only.

        A schema failure is never retried here: that is handled by validation,
        which rejects rather than re-rolls.
        """
        url = f"{_API_ROOT}/models/{self.model}:generateContent"
        client = await self._get_client()
        last_error: Exception | None = None

        for attempt in range(_MAX_ATTEMPTS):
            try:
                response = await client.post(
                    url,
                    json=payload,
                    headers={
                        "x-goog-api-key": self.api_key,
                        "Content-Type": "application/json",
                    },
                    timeout=self.timeout,
                )
            except httpx.TimeoutException as exc:
                last_error = AITimeout(
                    f"Gemini did not respond within {self.timeout}s.", detail=str(exc)
                )
            except httpx.HTTPError as exc:
                last_error = AITransportError("Could not reach Gemini.", detail=str(exc))
            else:
                if response.status_code == 429:
                    retry_after = _retry_after_seconds(response)
                    last_error = AIRateLimited(
                        "Gemini rate limit reached.",
                        retry_after=retry_after,
                        detail=_safe_error_detail(response),
                    )
                elif response.status_code >= 500:
                    last_error = AITransportError(
                        f"Gemini returned {response.status_code}.",
                        detail=_safe_error_detail(response),
                    )
                elif response.status_code >= 400:
                    # 4xx other than 429 will not succeed on a retry.
                    raise AIRequestRejected(
                        f"Gemini rejected the request ({response.status_code}).",
                        detail=_safe_error_detail(response),
                    )
                else:
                    return _extract_text(response.json())

            if attempt < _MAX_ATTEMPTS - 1:
                delay = _BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS) - 1)]
                if isinstance(last_error, AIRateLimited) and last_error.retry_after:
                    # Respect the provider's own backoff, but never stall a
                    # teacher's request for minutes.
                    delay = min(last_error.retry_after, 10.0)
                logger.warning(
                    "Retrying Gemini request",
                    extra={"attempt": attempt + 1, "delay_seconds": delay},
                )
                await asyncio.sleep(delay)

        assert last_error is not None
        raise last_error


def _retry_after_seconds(response: httpx.Response) -> float | None:
    header = response.headers.get("Retry-After")
    if not header:
        return None
    try:
        return float(header)
    except ValueError:
        return None


def _safe_error_detail(response: httpx.Response) -> str:
    """A short, non-sensitive slice of an error body.

    Truncated because provider errors can echo the prompt back, and the prompt
    is not something to write into logs wholesale.
    """
    try:
        message = response.json().get("error", {}).get("message", "")
    except (ValueError, AttributeError):
        message = response.text
    return str(message)[:300]


def _extract_text(body: dict[str, Any]) -> str:
    """Pull the model's text out of a generateContent response."""
    candidates = body.get("candidates") or []
    if not candidates:
        # Usually a safety block; there is no content to salvage.
        reason = (body.get("promptFeedback") or {}).get("blockReason")
        raise AIMalformedResponse(
            "Gemini returned no candidates.", detail=f"blockReason={reason}" if reason else None
        )
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(part.get("text", "") for part in parts).strip()
    if not text:
        raise AIMalformedResponse("Gemini returned an empty response.")
    return text


def _parse_variants(raw_text: str) -> list[RawVariant]:
    """Strict JSON parse into untyped candidates.

    Malformed output is rejected here rather than coerced into something that
    looks plausible. Only a markdown fence is stripped, because that is a
    transport artefact rather than a content problem.
    """
    text = raw_text.strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1)

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AIMalformedResponse(
            "Gemini did not return valid JSON.", detail=str(exc)[:200]
        ) from exc

    if not isinstance(parsed, dict) or not isinstance(parsed.get("variants"), list):
        raise AIMalformedResponse("Gemini response has no 'variants' array.")

    candidates: list[RawVariant] = []
    for entry in parsed["variants"]:
        if not isinstance(entry, dict):
            # Keep it as a candidate so validation reports it per-variant
            # rather than failing the whole batch.
            candidates.append(RawVariant(raw={"invalid": entry}))
            continue
        candidates.append(
            RawVariant(
                question_text=entry.get("question_text"),
                options=entry.get("options"),
                correct_option=entry.get("correct_option"),
                explanation=entry.get("explanation"),
                topic=entry.get("topic"),
                difficulty=entry.get("difficulty"),
                raw=entry,
            )
        )
    return candidates


__all__ = ["GeminiProvider"]
