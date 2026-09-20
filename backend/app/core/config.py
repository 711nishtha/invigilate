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
    gemini_model: str = "gemini-2.5-flash"
    ai_request_timeout_seconds: int = 60

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
    max_request_bytes: int = 262_144

    # --- Exam engine -------------------------------------------------------
    attempt_grace_seconds: int = 5
    default_variants_per_question: int = 5

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
        return [name for name, value in required.items() if not value]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
