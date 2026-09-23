"""Supabase Storage implementation.

The only module that knows Supabase Storage exists. Uses the REST API over
httpx with the **service-role key**, which is why this runs server-side only
and why that key never appears in any `VITE_`-prefixed variable.

Buckets are private. A teacher's download is authorized by the API first, and
the object is then either streamed through the backend or handed over as a
short-lived signed URL — never a public link.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.storage.base import (
    StorageProvider,
    StorageRejected,
    StorageUnavailable,
    StoredObject,
)

logger = get_logger("securequiz.storage.supabase")

DEFAULT_SIGNED_URL_TTL = 300  # 5 minutes: long enough to download, short enough to leak little


class SupabaseStorageProvider(StorageProvider):
    name = "supabase"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        service_role_key: str | None = None,
        timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = (base_url or settings.supabase_url or "").rstrip("/")
        self.service_role_key = (
            service_role_key
            if service_role_key is not None
            else settings.supabase_service_role_key
        )
        self.timeout = timeout
        self._client = client
        self._owns_client = client is None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.service_role_key}",
            "apikey": self.service_role_key,
        }

    def _require_config(self) -> None:
        if not self.base_url or not self.service_role_key:
            raise StorageRejected(
                "Supabase Storage is not configured on this server.",
            )

    async def upload(
        self, *, bucket: str, path: str, content: bytes, content_type: str
    ) -> StoredObject:
        self._require_config()
        client = await self._get_client()
        url = f"{self.base_url}/storage/v1/object/{bucket}/{path}"

        try:
            response = await client.post(
                url,
                content=content,
                headers={
                    **self._headers(),
                    "Content-Type": content_type,
                    # Overwrite: a regenerated export replaces the stale one
                    # rather than accumulating copies.
                    "x-upsert": "true",
                },
                timeout=self.timeout,
            )
        except httpx.TimeoutException as exc:
            raise StorageUnavailable("Supabase Storage timed out.", detail=str(exc)) from exc
        except httpx.HTTPError as exc:
            raise StorageUnavailable(
                "Could not reach Supabase Storage.", detail=str(exc)
            ) from exc

        if response.status_code >= 500:
            raise StorageUnavailable(
                f"Supabase Storage returned {response.status_code}.",
                detail=_safe_detail(response),
            )
        if response.status_code >= 400:
            raise StorageRejected(
                f"Supabase Storage rejected the upload ({response.status_code}).",
                detail=_safe_detail(response),
            )

        logger.info(
            "Uploaded an export",
            extra={"bucket": bucket, "path": path, "bytes": len(content)},
        )
        return StoredObject(bucket=bucket, path=path, size_bytes=len(content))

    async def signed_url(
        self, *, bucket: str, path: str, expires_in: int = DEFAULT_SIGNED_URL_TTL
    ) -> str:
        self._require_config()
        client = await self._get_client()
        url = f"{self.base_url}/storage/v1/object/sign/{bucket}/{path}"

        try:
            response = await client.post(
                url,
                json={"expiresIn": expires_in},
                headers=self._headers(),
                timeout=self.timeout,
            )
        except httpx.TimeoutException as exc:
            raise StorageUnavailable("Supabase Storage timed out.", detail=str(exc)) from exc
        except httpx.HTTPError as exc:
            raise StorageUnavailable(
                "Could not reach Supabase Storage.", detail=str(exc)
            ) from exc

        if response.status_code >= 500:
            raise StorageUnavailable(
                f"Supabase Storage returned {response.status_code}.",
                detail=_safe_detail(response),
            )
        if response.status_code >= 400:
            raise StorageRejected(
                f"Supabase Storage refused to sign ({response.status_code}).",
                detail=_safe_detail(response),
            )

        payload: dict[str, Any] = response.json()
        signed = payload.get("signedURL") or payload.get("signedUrl") or ""
        if not signed:
            raise StorageRejected("Supabase Storage returned no signed URL.")
        return f"{self.base_url}/storage/v1{signed}" if signed.startswith("/") else signed


def _safe_detail(response: httpx.Response) -> str:
    """A short, non-sensitive slice of an error body.

    Truncated because an error can echo request headers, and those carry the
    service-role key.
    """
    try:
        payload = response.json()
        message = payload.get("message") or payload.get("error") or ""
    except (ValueError, AttributeError):
        message = response.text
    return str(message)[:300]


__all__ = ["DEFAULT_SIGNED_URL_TTL", "SupabaseStorageProvider"]
