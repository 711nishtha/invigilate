"""Rate limiting and request-size limits."""

from __future__ import annotations

from fastapi import Request, Response, status
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from app.core.config import settings


def rate_limit_key(request: Request) -> str:
    """Per-attempt for exam traffic, per-IP otherwise.

    An exam generates a lot of legitimate answer and event traffic from one
    student. Keying those routes on the attempt token means one busy student
    cannot exhaust the budget of everybody behind the same campus NAT.
    """
    attempt_token = request.headers.get("X-Attempt-Token")
    if attempt_token:
        return f"attempt:{attempt_token[:16]}"
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return f"ip:{forwarded.split(',')[0].strip()}"
    return f"ip:{get_remote_address(request)}"


# `enabled` is deliberately not configurable. There is no environment variable
# that turns rate limiting off, so a production deploy cannot be talked out of
# it. The test suite toggles `limiter.enabled` in-process via a fixture; that
# attribute is never written by application code.
limiter = Limiter(
    key_func=rate_limit_key,
    default_limits=[settings.rate_limit_default],
    headers_enabled=True,
)


class RateLimitingDisabled(RuntimeError):
    """Raised at startup if production would run without rate limiting."""


def assert_rate_limiting_active() -> None:
    """Fail the boot rather than serve production traffic unprotected.

    Three ways rate limiting could silently vanish, all checked here:
    a test fixture leaking `enabled = False` into a real process, an empty
    `default_limits` list, and a limit string that parses to nothing. The
    settings validator already rejects the last two at load time; this is the
    belt-and-braces check at the point of no return.
    """
    problems: list[str] = []
    if not limiter.enabled:
        problems.append("limiter.enabled is False")
    if not limiter._default_limits:  # noqa: SLF001 - no public accessor exists
        problems.append("limiter has no default limits")
    if not settings.rate_limit_default.strip():
        problems.append("RATE_LIMIT_DEFAULT is empty")

    if problems:
        raise RateLimitingDisabled(
            "Rate limiting is not active: " + "; ".join(problems)
        )


def rate_limit_handler(request: Request, exc: RateLimitExceeded) -> Response:
    """Render a 429 in the documented error envelope.

    Deliberately synchronous. slowapi's middleware looks up the registered
    handler but discards it if it is a coroutine function, silently falling
    back to its own `{"error": "Rate limit exceeded: ..."}` string. A sync
    handler is used by both paths, so a 429 from a default limit and one from
    a per-route decorator have the same shape. Starlette runs sync handlers in
    a threadpool, so nothing blocks the event loop.
    """
    retry_after = getattr(exc, "retry_after", None) or 60
    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content={
            "error": {
                "code": "RATE_LIMITED",
                "message": "Too many requests. Please slow down.",
                "details": {"retry_after_seconds": retry_after},
            }
        },
        headers={"Retry-After": str(retry_after)},
    )


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject oversized bodies before they are parsed.

    Integrity event metadata is attacker-controlled, so an unbounded body is a
    cheap way to exhaust memory.
    """

    def __init__(self, app: ASGIApp, max_bytes: int | None = None) -> None:
        super().__init__(app)
        self.max_bytes = max_bytes or settings.max_request_bytes

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        content_length = request.headers.get("content-length")
        if content_length and content_length.isdigit() and int(content_length) > self.max_bytes:
            return JSONResponse(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                content={
                    "error": {
                        "code": "REQUEST_TOO_LARGE",
                        "message": "Request body exceeds the allowed size.",
                        "details": {"max_bytes": self.max_bytes},
                    }
                },
            )
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Conservative headers for a JSON API that is never framed."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cache-Control", "no-store")
        return response
