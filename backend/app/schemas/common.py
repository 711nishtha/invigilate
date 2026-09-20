"""Shared response primitives."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class APIModel(BaseModel):
    """Base for response models (ORM-attribute friendly)."""

    model_config = ConfigDict(from_attributes=True)


class StrictModel(BaseModel):
    """Base for request models.

    ``extra="forbid"`` means an unexpected field is a 422 rather than something
    silently ignored — which is how a client learns it cannot smuggle in
    ``role`` or ``severity``.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorDetail


class Page(APIModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class HealthResponse(BaseModel):
    status: str
    version: str
    environment: str
    server_time: datetime


class ReadinessResponse(HealthResponse):
    database: str
