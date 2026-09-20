"""Core primitives: clock, token minting, config, logging redaction."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.core import clock
from app.core.config import Settings
from app.core.logging import JsonFormatter, _redact
from app.core.security import (
    attempt_token_matches,
    generate_attempt_seed,
    generate_attempt_token,
    generate_public_code,
    hash_attempt_token,
)


class TestClock:
    def test_utcnow_is_timezone_aware(self) -> None:
        assert clock.utcnow().tzinfo is not None

    def test_expiry_is_start_plus_duration(self) -> None:
        start = clock.utcnow()
        assert clock.expiry_from(start, 1200) == start + timedelta(seconds=1200)

    def test_future_expiry_is_not_expired(self) -> None:
        assert not clock.is_expired(clock.utcnow() + timedelta(minutes=5))

    def test_past_expiry_is_expired(self) -> None:
        assert clock.is_expired(clock.utcnow() - timedelta(seconds=1))

    def test_grace_window_covers_an_answer_in_flight(self) -> None:
        just_past = clock.utcnow() - timedelta(seconds=3)
        assert clock.is_expired(just_past)
        assert not clock.is_expired(just_past, grace_seconds=10)

    def test_seconds_remaining_never_goes_negative(self) -> None:
        assert clock.seconds_remaining(clock.utcnow() - timedelta(hours=1)) == 0

    def test_naive_datetimes_are_treated_as_utc(self) -> None:
        from datetime import datetime

        assert clock.ensure_aware(datetime(2026, 1, 1)).tzinfo is not None


class TestAttemptTokens:
    def test_tokens_are_unique_and_long(self) -> None:
        tokens = {generate_attempt_token() for _ in range(200)}
        assert len(tokens) == 200
        assert all(len(token) >= 32 for token in tokens)

    def test_only_the_hash_needs_storing(self) -> None:
        token = generate_attempt_token()
        stored = hash_attempt_token(token)
        assert token not in stored
        assert len(stored) == 64
        assert attempt_token_matches(token, stored)

    def test_a_different_token_does_not_match(self) -> None:
        stored = hash_attempt_token(generate_attempt_token())
        assert not attempt_token_matches(generate_attempt_token(), stored)


class TestPublicCodes:
    def test_code_shape(self) -> None:
        code = generate_public_code()
        assert len(code) == 6
        assert code.isalnum() and code.isupper()

    def test_ambiguous_characters_are_excluded(self) -> None:
        codes = "".join(generate_public_code() for _ in range(300))
        assert not set(codes) & set("OI01L")

    def test_codes_are_not_sequential(self) -> None:
        codes = [generate_public_code() for _ in range(500)]
        assert len(set(codes)) > 495  # collisions at this volume would be alarming


class TestSeeds:
    def test_seed_fits_in_a_signed_bigint(self) -> None:
        for _ in range(100):
            assert 0 <= generate_attempt_seed() < 2**63

    def test_seeds_differ_between_students(self) -> None:
        assert len({generate_attempt_seed() for _ in range(200)}) == 200


class TestSettings:
    def test_cors_origins_accept_a_comma_separated_string(self) -> None:
        settings = Settings(cors_origins="https://a.example, https://b.example")  # type: ignore[arg-type]
        assert settings.cors_origins == ["https://a.example", "https://b.example"]

    def test_docs_are_disabled_in_production(self) -> None:
        assert Settings(environment="production").docs_url is None
        assert Settings(environment="development").docs_url == "/docs"

    def test_missing_production_secrets_are_reported(self) -> None:
        settings = Settings(
            environment="production",
            supabase_url="",
            supabase_jwt_secret="",
            supabase_service_role_key="",
            ai_provider="stub",
            email_provider="stub",
        )
        missing = settings.missing_production_secrets()
        assert "SUPABASE_JWT_SECRET" in missing
        assert "SUPABASE_SERVICE_ROLE_KEY" in missing

    def test_alembic_url_falls_back_to_database_url(self) -> None:
        settings = Settings(database_url="postgresql+psycopg://x/y", alembic_database_url=None)
        assert settings.migration_database_url == "postgresql+psycopg://x/y"


class TestLogRedaction:
    @pytest.mark.parametrize(
        "key",
        ["password", "supabase_jwt_secret", "api_key", "authorization", "attempt_token", "correct_option"],
    )
    def test_sensitive_keys_are_redacted(self, key: str) -> None:
        assert _redact("sensitive", key) == "[REDACTED]"

    def test_nested_structures_are_redacted(self) -> None:
        payload = {"context": {"gemini_api_key": "secret", "safe": "visible"}}
        redacted = _redact(payload, "context")
        assert redacted["context"]["gemini_api_key"] == "[REDACTED]"
        assert redacted["context"]["safe"] == "visible"

    def test_ordinary_fields_survive(self) -> None:
        assert _redact("quiz-123", "quiz_id") == "quiz-123"

    def test_formatter_emits_json(self) -> None:
        import json
        import logging

        record = logging.LogRecord("t", logging.INFO, __file__, 1, "ATTEMPT_STARTED", (), None)
        record.attempt_id = "abc"  # type: ignore[attr-defined]
        record.api_key = "leak-me"  # type: ignore[attr-defined]
        parsed = json.loads(JsonFormatter().format(record))
        assert parsed["message"] == "ATTEMPT_STARTED"
        assert parsed["attempt_id"] == "abc"
        assert parsed["api_key"] == "[REDACTED]"
