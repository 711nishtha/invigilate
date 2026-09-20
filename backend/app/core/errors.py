"""Uniform API error handling.

Every failure leaves the API in the shape documented in docs/api.md:
    {"error": {"code": ..., "message": ..., "details": {...}}}
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger

logger = get_logger("securequiz.error")


class AppError(Exception):
    """Base class for errors that map to a deliberate API response."""

    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "BAD_REQUEST"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code
        self.details = details or {}


class NotAuthenticated(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "NOT_AUTHENTICATED"


class PermissionDenied(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "PERMISSION_DENIED"


class NotFound(AppError):
    """Also used to hide resources the caller may not see.

    Returning 404 rather than 403 for someone else's quiz means IDs cannot be
    probed for existence.
    """

    status_code = status.HTTP_404_NOT_FOUND
    code = "NOT_FOUND"


class Conflict(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "CONFLICT"


class AttemptExpired(AppError):
    status_code = status.HTTP_410_GONE
    code = "ATTEMPT_EXPIRED"


def _serializable_errors(exc: RequestValidationError) -> list[dict[str, Any]]:
    """Make Pydantic's error list safe to serialize.

    A custom field validator that raises ValueError puts the exception object
    itself into ``ctx``, which json.dumps cannot encode — turning a 422 into a
    500. Stringifying ctx keeps the useful detail without the object.
    """
    cleaned: list[dict[str, Any]] = []
    for error in exc.errors():
        entry = {
            "type": error.get("type"),
            "loc": [str(part) for part in error.get("loc", ())],
            "msg": str(error.get("msg", "")),
        }
        context = error.get("ctx")
        if context:
            entry["ctx"] = {key: str(value) for key, value in context.items()}
        cleaned.append(entry)
    return cleaned


def _error_response(
    status_code: int, code: str, message: str, details: dict[str, Any] | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "details": details or {}}},
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return _error_response(exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return _error_response(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "VALIDATION_ERROR",
            "Request validation failed.",
            {"errors": _serializable_errors(exc)},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {
            401: "NOT_AUTHENTICATED",
            403: "PERMISSION_DENIED",
            404: "NOT_FOUND",
            405: "METHOD_NOT_ALLOWED",
            413: "REQUEST_TOO_LARGE",
            429: "RATE_LIMITED",
        }.get(exc.status_code, "HTTP_ERROR")
        return _error_response(exc.status_code, code, str(exc.detail))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Log the detail; return none of it. Stack traces are not a public API.
        logger.exception(
            "Unhandled error", extra={"path": request.url.path, "method": request.method}
        )
        return _error_response(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "INTERNAL_ERROR",
            "An unexpected error occurred.",
        )
