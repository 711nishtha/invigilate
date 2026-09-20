"""Liveness and readiness probes."""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from app.core.clock import utcnow
from app.core.config import settings
from app.db.session import check_database
from app.schemas.common import HealthResponse, ReadinessResponse

router = APIRouter(prefix="/health", tags=["health"])


@router.get("", response_model=HealthResponse, summary="Liveness")
def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        version=settings.app_version,
        environment=settings.environment,
        server_time=utcnow(),
    )


@router.get("/ready", response_model=ReadinessResponse, summary="Readiness")
def ready(response: Response) -> ReadinessResponse:
    """Render uses this to decide whether an instance should take traffic.

    A database that is unreachable makes the instance unready, not dead.
    """
    db_ok = check_database()
    if not db_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(
        status="ok" if db_ok else "degraded",
        version=settings.app_version,
        environment=settings.environment,
        server_time=utcnow(),
        database="ok" if db_ok else "unavailable",
    )
