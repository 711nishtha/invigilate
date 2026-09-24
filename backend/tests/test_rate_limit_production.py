"""Rate limiting is a production control, not a preference.

The test suite disables the limiter for convenience, which is exactly the kind
of convenience that leaks into production if nobody guards it. These tests
assert the guard holds:

* application code never writes ``limiter.enabled``
* no environment variable can turn limiting off
* a production process that would run unprotected refuses to start
* the test fixture's state does not leak between cases
"""

from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.rate_limit import RateLimitingDisabled, assert_rate_limiting_active, limiter


class TestDisablingIsTestOnly:
    def test_application_code_never_disables_the_limiter(self) -> None:
        """Only tests may write `limiter.enabled`. This is the actual guarantee."""
        offenders = []
        for path in pathlib.Path("app").rglob("*.py"):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if "limiter.enabled" in stripped and "=" in stripped.split("limiter.enabled")[1][:3]:
                    offenders.append(f"{path}:{number}: {stripped}")
        assert not offenders, f"application code assigns limiter.enabled: {offenders}"

    def test_the_limiter_is_enabled_by_default(self) -> None:
        """Nothing in app startup turns it off, so the default must be on."""
        from slowapi import Limiter

        fresh = Limiter(key_func=lambda request: "k", default_limits=["1/minute"])
        assert fresh.enabled is True

    def test_no_setting_exists_to_disable_rate_limiting(self) -> None:
        """A deploy cannot be talked out of rate limiting by configuration."""
        fields = set(Settings.model_fields)
        assert not {"rate_limit_enabled", "disable_rate_limiting", "rate_limiting"} & fields


class TestConfigurationCannotSilentlyDisableIt:
    @pytest.mark.parametrize("bad", ["", "   ", "none", "unlimited", "0", "abc/minute"])
    def test_an_unusable_rate_limit_is_rejected_at_load(self, bad: str) -> None:
        """An empty RATE_LIMIT_DEFAULT would make default_limits an empty list."""
        with pytest.raises(Exception) as caught:
            Settings(rate_limit_default=bad)
        assert "rate limit" in str(caught.value).lower()

    @pytest.mark.parametrize("good", ["120/minute", "10 per minute", "5/second;100/hour"])
    def test_valid_limit_strings_are_accepted(self, good: str) -> None:
        assert Settings(rate_limit_default=good).rate_limit_default == good

    def test_every_rate_limit_setting_is_validated(self) -> None:
        for field in (
            "rate_limit_default",
            "rate_limit_attempt_start",
            "rate_limit_answer",
            "rate_limit_event",
            "rate_limit_generation",
        ):
            with pytest.raises(Exception):
                Settings(**{field: ""})

    def test_limits_come_from_settings_not_hardcoded_strings(self) -> None:
        """The generation route must read its limit from configuration."""
        source = pathlib.Path("app/api/routes/questions.py").read_text(encoding="utf-8")
        assert "settings.rate_limit_generation" in source


class TestProductionStartupAssertion:
    def test_the_assertion_passes_with_the_limiter_active(self) -> None:
        previous = limiter.enabled
        limiter.enabled = True
        try:
            assert_rate_limiting_active()
        finally:
            limiter.enabled = previous

    def test_production_refuses_to_start_with_the_limiter_disabled(self) -> None:
        """A leaked `enabled = False` must stop the process, not pass silently."""
        previous = limiter.enabled
        limiter.enabled = False
        try:
            with pytest.raises(RateLimitingDisabled) as caught:
                assert_rate_limiting_active()
            assert "limiter.enabled is False" in str(caught.value)
        finally:
            limiter.enabled = previous

    def test_production_refuses_to_start_with_no_default_limits(self) -> None:
        previous = limiter._default_limits  # noqa: SLF001
        limiter._default_limits = []  # noqa: SLF001
        try:
            with pytest.raises(RateLimitingDisabled):
                assert_rate_limiting_active()
        finally:
            limiter._default_limits = previous  # noqa: SLF001

    def test_startup_calls_the_assertion_for_production_and_staging(self) -> None:
        source = pathlib.Path("app/main.py").read_text(encoding="utf-8")
        assert "assert_rate_limiting_active()" in source
        assert '"production", "staging"' in source


class TestNoStateLeakBetweenTests:
    """The fixture must not let one test's limiter state affect another."""

    def test_limiter_is_disabled_here(self) -> None:
        assert limiter.enabled is False, "the autouse fixture should have disabled it"

    def test_limiter_is_still_disabled_in_the_next_test(self) -> None:
        """If the previous test leaked `enabled = True`, this fails."""
        assert limiter.enabled is False

    def test_the_opt_in_fixture_enables_it(self, rate_limited) -> None:  # type: ignore[no-untyped-def]
        assert limiter.enabled is True

    def test_state_is_restored_after_the_opt_in_fixture(self) -> None:
        """Runs after the test above; proves the enable did not leak."""
        assert limiter.enabled is False


class TestLimitsAreStillEnforcedWhenEnabled:
    def test_the_default_limit_applies_to_an_ordinary_route(
        self, client: TestClient, rate_limited  # type: ignore[no-untyped-def]
    ) -> None:
        """With production configuration, an unlisted route still has a budget."""
        statuses = {client.get("/api/health").status_code for _ in range(130)}
        assert 429 in statuses, "the default limit must apply to routes with no decorator"

    def test_a_429_carries_the_documented_error_shape(
        self, client: TestClient, rate_limited  # type: ignore[no-untyped-def]
    ) -> None:
        last = None
        for _ in range(130):
            last = client.get("/api/health")
            if last.status_code == 429:
                break
        assert last is not None and last.status_code == 429
        assert last.json()["error"]["code"] == "RATE_LIMITED"
        assert last.headers.get("Retry-After")


class TestStubProvidersCannotRunInProduction:
    """The stubs exist so tests never call a paid API. They must stay in tests."""

    def test_stub_ai_is_flagged_as_a_production_misconfiguration(self) -> None:
        problems = Settings(environment="production", ai_provider="stub").production_misconfigurations()
        assert any("AI_PROVIDER=stub" in p for p in problems)

    def test_stub_email_is_flagged(self) -> None:
        problems = Settings(
            environment="production", email_provider="stub"
        ).production_misconfigurations()
        assert any("EMAIL_PROVIDER=stub" in p for p in problems)

    def test_real_providers_are_not_flagged(self) -> None:
        """Real providers plus real URLs is the only clean production shape."""
        settings = Settings(**PRODUCTION_READY)
        assert settings.production_misconfigurations() == []


# A production configuration with nothing left at a development default.
# `_env_file=None` keeps these hermetic: without it, Settings() reads a
# developer's backend/.env and the assertions depend on whatever is on their
# machine.
PRODUCTION_READY = dict(
    _env_file=None,
    environment="production",
    ai_provider="gemini",
    email_provider="resend",
    database_url="postgresql+psycopg://u:p@aws-0-eu-west-2.pooler.supabase.com:6543/postgres",
    frontend_url="https://quiz.example.edu",
    cors_origins=["https://quiz.example.edu"],
)


class TestDevelopmentDefaultsCannotReachProduction:
    """Every URL has a localhost default so a laptop just works.

    Those defaults are non-empty, so a missing-secret check cannot catch them:
    a deploy that forgot DATABASE_URL would start, point at a localhost
    database and 500 on every request.
    """

    def test_a_localhost_database_is_refused(self) -> None:
        settings = Settings(**{**PRODUCTION_READY, "database_url": "postgresql+psycopg://postgres:postgres@localhost:5432/secure_quiz"})
        assert any("DATABASE_URL" in p for p in settings.production_misconfigurations())

    def test_a_localhost_frontend_url_is_refused(self) -> None:
        """Result emails would link somewhere only the developer can open."""
        settings = Settings(**{**PRODUCTION_READY, "frontend_url": "http://localhost:5173"})
        assert any("FRONTEND_URL" in p for p in settings.production_misconfigurations())

    def test_localhost_cors_origins_are_refused(self) -> None:
        """The deployed frontend would be CORS-blocked by its own backend."""
        settings = Settings(**{**PRODUCTION_READY, "cors_origins": ["http://localhost:5173"]})
        assert any("CORS_ORIGINS" in p for p in settings.production_misconfigurations())

    def test_empty_cors_origins_are_refused(self) -> None:
        settings = Settings(**{**PRODUCTION_READY, "cors_origins": []})
        assert any("CORS_ORIGINS" in p for p in settings.production_misconfigurations())

    def test_a_forgotten_deploy_is_caught_completely(self) -> None:
        """Operator sets the secrets but leaves every URL at its dev value.

        The localhost values are stated explicitly rather than relying on the
        defaults: conftest puts DATABASE_URL in os.environ, which outranks the
        model default, so an implicit version of this test would silently
        assert less than it appears to.
        """
        settings = Settings(
            _env_file=None,
            environment="production",
            ai_provider="gemini",
            email_provider="resend",
            database_url="postgresql+psycopg://postgres:postgres@localhost:5432/secure_quiz",
            frontend_url="http://localhost:5173",
            cors_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        )
        problems = settings.production_misconfigurations()
        assert len(problems) == 3, problems
        assert {"DATABASE_URL", "FRONTEND_URL", "CORS_ORIGINS"} == {
            problem.split()[0] for problem in problems
        }

    def test_development_is_unaffected(self) -> None:
        """Localhost is correct on a laptop; only startup for prod/staging checks."""
        import pathlib as _pathlib

        source = _pathlib.Path("app/main.py").read_text(encoding="utf-8")
        assert '"production", "staging"' in source
        assert "production_misconfigurations()" in source

    def test_startup_refuses_an_unsafe_production_configuration(self) -> None:
        source = pathlib.Path("app/main.py").read_text(encoding="utf-8")
        assert "production_misconfigurations()" in source
        assert "Unsafe production configuration" in source
