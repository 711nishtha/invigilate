"""In-memory storage for tests and local development.

Keeps uploads in a dict so a test can assert on them. No network, no bucket, no
credentials.
"""

from __future__ import annotations

from app.storage.base import StorageError, StorageProvider, StoredObject


class StubStorageProvider(StorageProvider):
    name = "stub"

    def __init__(self, *, fail_with: StorageError | None = None) -> None:
        self.fail_with = fail_with
        self.objects: dict[str, bytes] = {}

    async def upload(
        self, *, bucket: str, path: str, content: bytes, content_type: str
    ) -> StoredObject:
        if self.fail_with is not None:
            raise self.fail_with
        self.objects[f"{bucket}/{path}"] = content
        return StoredObject(bucket=bucket, path=path, size_bytes=len(content))

    async def signed_url(self, *, bucket: str, path: str, expires_in: int) -> str:
        if self.fail_with is not None:
            raise self.fail_with
        return f"https://stub.storage.local/{bucket}/{path}?expires_in={expires_in}"

    async def delete(self, *, bucket: str, path: str) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        # Idempotent: a missing key is a success, matching the real provider.
        self.objects.pop(f"{bucket}/{path}", None)
