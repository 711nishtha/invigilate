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


limiter = Limiter(
    key_func=rate_limit_key,
    default_limits=[settings.rate_limit_default],
    headers_enabled=True,
)


async def rate_limit_handler(request: Request, exc: RateLimitExceeded) -> Response:
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
