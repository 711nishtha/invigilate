"""Storage provider selection."""

from __future__ import annotations

from app.core.config import settings
from app.storage.base import StorageProvider

_override: StorageProvider | None = None


def set_storage_provider(provider: StorageProvider | None) -> None:
    """Install a provider for the process. Used by tests to inject a stub."""
    global _override
    _override = provider


def get_storage_provider() -> StorageProvider:
    if _override is not None:
        return _override

    if settings.storage_provider == "stub":
        from app.storage.stub import StubStorageProvider

        return StubStorageProvider()

    from app.storage.supabase_storage import SupabaseStorageProvider

    return SupabaseStorageProvider()
