"""Application configuration.

Every secret and every environment-dependent value enters the process here and
nowhere else. Nothing in the codebase reads os.environ directly.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "test", "staging", "production"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Runtime -----------------------------------------------------------
    environment: Environment = "development"
    log_level: str = "INFO"
    port: int = 8000
    app_name: str = "Secure Quiz API"
    app_version: str = "0.1.0"

    # --- Database ----------------------------------------------------------
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/secure_quiz"
    alembic_database_url: str | None = None
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_echo: bool = False

    # --- Supabase ----------------------------------------------------------
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_service_role_key: str = ""
    supabase_jwt_secret: str = ""
    supabase_jwt_audience: str = "authenticated"
    supabase_storage_bucket_evidence: str = "evidence"
    supabase_storage_bucket_exports: str = "exports"

    # --- AI ----------------------------------------------------------------
    ai_provider: Literal["gemini", "stub"] = "gemini"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.1-flash-lite"
    # Measured against the live API: a flash-lite generation takes ~55-70s and
    # the cost is dominated by fixed model overhead, not by variant count
    # (1 variant 62s, 5 variants 68s). The previous 60s default timed out on
    # essentially every real call. 120s leaves headroom without making a failed
    # call unbearable to wait out.
    ai_request_timeout_seconds: int = 120

    # --- Storage -----------------------------------------------------------
    storage_provider: Literal["supabase", "stub"] = "supabase"
    # Retain generated exports in Supabase Storage. Off by default: the
    # workbook is regenerable from the database at any time, so retention is a
    # convenience rather than a requirement, and not storing student marks is
    # the safer default.
    retain_exports: bool = False
    export_signed_url_ttl_seconds: int = 300

    # --- Email -------------------------------------------------------------
    email_provider: Literal["resend", "stub"] = "resend"
    resend_api_key: str = ""
    email_from: str = "Secure Quiz <results@example.com>"

    # --- Web ---------------------------------------------------------------
    frontend_url: str = "http://localhost:5173"
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"]
    )

    # --- Hardening ---------------------------------------------------------
    rate_limit_default: str = "120/minute"
    rate_limit_attempt_start: str = "10/minute"
    rate_limit_answer: str = "240/minute"
    rate_limit_event: str = "300/minute"
    rate_limit_generation: str = "20/minute"
    # Export builds a whole workbook; email sends one. Both are cheap to
    # trigger and expensive to serve, and a double-click must not become a
    # mail storm.
    rate_limit_export: str = "10/minute"
    rate_limit_email: str = "5/minute"
    max_request_bytes: int = 262_144

    # --- Exam engine -------------------------------------------------------
    attempt_grace_seconds: int = 5
    default_variants_per_question: int = 5

    @field_validator(
        "rate_limit_default",
        "rate_limit_attempt_start",
        "rate_limit_answer",
        "rate_limit_event",
        "rate_limit_generation",
        "rate_limit_export",
        "rate_limit_email",
    )
    @classmethod
    def _validate_rate_limit(cls, value: str) -> str:
        """Reject a rate limit that would silently disable limiting.

        An empty string makes slowapi's default_limits an empty list, which
        turns rate limiting off with no error at all — exactly the kind of
        misconfiguration a blank Render environment variable produces. Parsing
        the value here means a typo fails at boot instead of quietly removing a
        control.
        """
        from limits import parse_many

        text = (value or "").strip()
        if not text:
            raise ValueError(
                "Rate limit must not be empty. Use a value like '120/minute'; "
                "rate limiting cannot be disabled by configuration."
            )
        try:
            parsed = parse_many(text)
        except ValueError as exc:
            raise ValueError(f"Invalid rate limit {text!r}: {exc}") from exc
        if not parsed:
            raise ValueError(f"Invalid rate limit {text!r}: no limit parsed.")
        return text

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        """Accept a comma-separated string, which is how Render passes lists."""
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def docs_url(self) -> str | None:
        """OpenAPI docs are a reconnaissance aid; they stay off in production."""
        return None if self.is_production else "/docs"

    @property
    def migration_database_url(self) -> str:
        """Alembic needs the direct connection, not the pgBouncer pooler."""
        return self.alembic_database_url or self.database_url

    def production_misconfigurations(self) -> list[str]:
        """Settings that are valid but must never be used in production.

        Two classes of problem, both of which would otherwise boot cleanly and
        fail confusingly later:

        *Stub providers.* They exist so tests never call a paid API. Left
        enabled on a real deploy they would silently hand teachers fabricated
        questions and silently drop result emails, with no error anywhere.

        *Development defaults.* Every URL below has a localhost default so that
        `uvicorn app.main:app` just works on a laptop. Those defaults are
        non-empty, so a missing-secret check cannot catch them: a deploy that
        forgot DATABASE_URL would start, point at a localhost database, and
        500 on every request. CORS would reject the real frontend, and result
        emails would link to localhost.
        """
        problems: list[str] = []

        localhost_markers = ("localhost", "127.0.0.1", "0.0.0.0")

        def is_local(value: str) -> bool:
            return any(marker in value for marker in localhost_markers)

        if is_local(self.database_url):
            problems.append(
                "DATABASE_URL still points at localhost; set the Supabase connection string"
            )
        if is_local(self.frontend_url):
            problems.append(
                "FRONTEND_URL still points at localhost; result email links would be unusable"
            )
        if not self.cors_origins or any(is_local(origin) for origin in self.cors_origins):
            problems.append(
                "CORS_ORIGINS still contains localhost; the deployed frontend would be blocked"
            )
        if self.ai_provider == "stub":
            problems.append(
                "AI_PROVIDER=stub generates fake questions and must not run in production"
            )
        if self.email_provider == "stub":
            problems.append(
                "EMAIL_PROVIDER=stub silently discards email and must not run in production"
            )
        if self.storage_provider == "stub" and self.retain_exports:
            problems.append(
                "STORAGE_PROVIDER=stub keeps exports in memory only and must not run "
                "in production when RETAIN_EXPORTS is enabled"
            )
        return problems

    def missing_production_secrets(self) -> list[str]:
        """Names of required settings that are still empty.

        Called at startup so a misconfigured production deploy fails loudly
        instead of failing on the first teacher login.
        """
        required = {
            "DATABASE_URL": self.database_url,
            "SUPABASE_URL": self.supabase_url,
            "SUPABASE_JWT_SECRET": self.supabase_jwt_secret,
            "SUPABASE_SERVICE_ROLE_KEY": self.supabase_service_role_key,
        }
        if self.ai_provider == "gemini":
            required["GEMINI_API_KEY"] = self.gemini_api_key
        if self.email_provider == "resend":
            required["RESEND_API_KEY"] = self.resend_api_key
        if self.retain_exports and self.storage_provider == "supabase":
            required["SUPABASE_SERVICE_ROLE_KEY"] = self.supabase_service_role_key
        return [name for name, value in required.items() if not value]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
