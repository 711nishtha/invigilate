"""Live Gemini smoke test.

A tiny, controlled, real API call — deliberately not the test suite. Run it
once after configuring a key, and again whenever the model or adapter changes.

    cd backend
    GEMINI_API_KEY=... ./.venv/Scripts/python.exe scripts/gemini_smoke.py

It goes through the ordinary :class:`AIProvider` abstraction, so what it proves
is what the application actually does. It never falls back to the stub: if no
key is configured it says so and exits non-zero, because a smoke test that
quietly passes without calling anything is worse than no smoke test.

Exit codes: 0 verified, 1 failed, 2 not attempted (no credential).
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

# Configure before app modules read settings. The smoke test talks to the real
# provider by definition, so the provider is pinned here.
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ["AI_PROVIDER"] = "gemini"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ai.base import (  # noqa: E402
    AIMalformedResponse,
    AIProviderError,
    AIRateLimited,
    AIRequestRejected,
    AITimeout,
    AITransportError,
    VariantGenerationRequest,
)
from app.ai.gemini import GeminiProvider  # noqa: E402
from app.ai.validation import validate_batch  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.db.models import QuestionType  # noqa: E402

CHECKS: list[tuple[str, str, str]] = []


def record(name: str, status: str, detail: str = "") -> None:
    CHECKS.append((name, status, detail))
    marker = {"VERIFIED": "  ok  ", "FAILED": " FAIL ", "SKIP": " skip "}.get(status, "  ?   ")
    print(f"[{marker}] {name}" + (f" — {detail}" if detail else ""))


async def main() -> int:
    key = settings.gemini_api_key
    print(f"model: {settings.gemini_model}")
    print(f"timeout: {settings.ai_request_timeout_seconds}s")
    print(f"key configured: {'yes (len %d)' % len(key) if key else 'NO'}\n")

    if not key:
        record(
            "credential",
            "SKIP",
            "GEMINI_API_KEY is not set. Live verification NOT ATTEMPTED.",
        )
        print(
            "\nNo API key configured. This smoke test does not substitute a stub;\n"
            "set GEMINI_API_KEY and re-run to verify the provider for real."
        )
        return 2

    provider = GeminiProvider()
    request = VariantGenerationRequest(
        original_question="What is the time complexity of binary search on a sorted array?",
        question_type=QuestionType.MCQ,
        # Deliberately tiny: two variants is enough to prove the contract and
        # keeps the token cost of a smoke test negligible.
        count=2,
        topic="Algorithms",
        difficulty="medium",
        option_count=4,
    )

    started = time.monotonic()
    try:
        batch = await provider.generate_question_variants(request)
    except AIRequestRejected as exc:
        record("authentication / request accepted", "FAILED", f"{exc.message} | {exc.detail}")
        print("\nA 4xx is not retryable. Check the key, the model name and the request shape.")
        await provider.aclose()
        return 1
    except AITimeout as exc:
        record("timeout handling", "FAILED", f"mapped correctly but timed out: {exc.message}")
        await provider.aclose()
        return 1
    except AIRateLimited as exc:
        record("rate-limit handling", "FAILED", f"429 after retries: retry_after={exc.retry_after}")
        await provider.aclose()
        return 1
    except AITransportError as exc:
        record("transport", "FAILED", f"{exc.message} | {exc.detail}")
        await provider.aclose()
        return 1
    except AIMalformedResponse as exc:
        record("structured response", "FAILED", f"{exc.message} | {exc.detail}")
        await provider.aclose()
        return 1
    except AIProviderError as exc:  # pragma: no cover - defensive
        record("provider", "FAILED", f"{type(exc).__name__}: {exc.message}")
        await provider.aclose()
        return 1
    finally:
        elapsed = time.monotonic() - started

    record("authentication", "VERIFIED", "API accepted the key")
    record("model availability", "VERIFIED", f"{batch.model} responded in {elapsed:.2f}s")
    record("request format", "VERIFIED", "generateContent accepted the payload")
    record(
        "structured response",
        "VERIFIED" if batch.variants else "FAILED",
        f"{len(batch.variants)} candidate(s) parsed from JSON",
    )
    record("prompt provenance", "VERIFIED", f"prompt_version={batch.prompt_version}")

    # The response shape only matters insofar as the backend can validate it.
    result = validate_batch(
        batch.variants,
        question_type=QuestionType.MCQ,
        existing=[],
        existing_hashes=set(),
        option_count=4,
        fallback_topic="Algorithms",
        fallback_difficulty="medium",
    )
    accepted, rejected = len(result.accepted), len(result.rejected)
    record(
        "response shape passes backend validation",
        "VERIFIED" if accepted else "FAILED",
        f"{accepted} accepted, {rejected} rejected by deterministic validation",
    )
    for verdict in result.rejected:
        reasons = ", ".join(f.reason.value for f in verdict.failures)
        print(f"          candidate {verdict.index}: {reasons}")

    if result.accepted:
        sample = result.accepted[0]
        print("\n  sample accepted variant")
        print(f"    question : {sample.question_text[:100]}")
        print(f"    options  : {len(sample.options or [])}")
        print(f"    correct  : index {sample.correct_option} (never sent to a student)")
        print(f"    topic    : {sample.topic} / {sample.difficulty}")

    # Observable cost signal. The REST response carries usageMetadata when the
    # model reports it; absence is not a failure, just nothing to observe.
    raw = batch.raw_text or ""
    record(
        "token/cost visibility",
        "VERIFIED" if raw else "SKIP",
        f"{len(raw)} chars of response text (usageMetadata is not surfaced by the adapter)",
    )

    await provider.aclose()

    failures = [c for c in CHECKS if c[1] == "FAILED"]
    print()
    if failures:
        print(f"RESULT: FAILED ({len(failures)} check(s))")
        return 1
    print("RESULT: VERIFIED — a real Gemini call succeeded and passed backend validation")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
