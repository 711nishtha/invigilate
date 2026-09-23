"""Object storage abstraction.

Exports must not live on the Render container filesystem: it is destroyed on
every deploy. When retention is configured they go to Supabase Storage;
otherwise the workbook is streamed to the teacher and never retained.

Nothing outside ``app/storage/`` knows which backend is in use.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class StorageError(Exception):
    """Base class for storage failures.

    A storage failure must never cost a result: callers record it and continue,
    because the workbook can always be regenerated from the database.
    """

    retryable: bool = False

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class StorageUnavailable(StorageError):
    retryable = True


class StorageRejected(StorageError):
    """Bad credentials, missing bucket, object too large."""

    retryable = False


@dataclass(frozen=True)
class StoredObject:
    bucket: str
    path: str
    size_bytes: int


class StorageProvider(ABC):
    name: str = "abstract"

    @abstractmethod
    async def upload(
        self, *, bucket: str, path: str, content: bytes, content_type: str
    ) -> StoredObject:
        """Store an object, overwriting any existing one at that path."""

    @abstractmethod
    async def signed_url(self, *, bucket: str, path: str, expires_in: int) -> str:
        """A short-lived URL for one object.

        Signed rather than public: an exports bucket holds student names,
        answers and marks, so it is private and every download is authorized
        server-side first.
        """

    async def aclose(self) -> None:
        """Release transport resources. Safe to call more than once."""
