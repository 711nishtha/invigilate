"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.api.routes import auth, health, questions, quizzes, results, student
from app.core.config import settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.rate_limit import (
    RequestSizeLimitMiddleware,
    SecurityHeadersMiddleware,
    assert_rate_limiting_active,
    limiter,
    rate_limit_handler,
)

logger = get_logger("securequiz.app")


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    configure_logging()
    missing = settings.missing_production_secrets()
    if missing:
        message = "Missing required configuration: " + ", ".join(missing)
        if settings.is_production:
            # Fail at boot rather than at the first teacher login.
            raise RuntimeError(message)
        logger.warning(message, extra={"missing": missing})
    # Rate limiting is a production control, not a preference. A process that
    # would serve real traffic without it does not start. The same applies to
    # the stub providers, which exist only so tests never call a paid API.
    if settings.environment in ("production", "staging"):
        assert_rate_limiting_active()
        misconfigured = settings.production_misconfigurations()
        if misconfigured:
            raise RuntimeError("Unsafe production configuration: " + "; ".join(misconfigured))

    logger.info(
        "Application starting",
        extra={
            "environment": settings.environment,
            "version": settings.app_version,
            "rate_limiting": limiter.enabled,
            "rate_limit_default": settings.rate_limit_default,
        },
    )
    yield
    logger.info("Application shutting down")


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=(
            "Secure online assessment with integrity monitoring. "
            "The backend is authoritative for question assignment, timing, "
            "grading and submission."
        ),
        docs_url=settings.docs_url,
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
        lifespan=lifespan,
    )

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_handler)
    register_exception_handlers(app)

    # Order matters: the outermost middleware is added last.
    app.add_middleware(SlowAPIMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestSizeLimitMiddleware)
    app.add_middleware(
        CORSMiddleware,
        # Explicit allowlist. Never "*" — these requests carry credentials.
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Attempt-Token"],
        expose_headers=["Retry-After"],
        max_age=600,
    )

    api = APIRouter(prefix="/api")
    api.include_router(health.router)
    api.include_router(auth.router)
    api.include_router(quizzes.router)
    api.include_router(questions.quiz_scoped)
    api.include_router(questions.family_scoped)
    api.include_router(questions.variant_scoped)
    api.include_router(results.router)
    api.include_router(student.public_router)
    api.include_router(student.student_router)
    app.include_router(api)

    return app


app = create_app()
