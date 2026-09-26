"""The seam for a real desktop lockdown client. Deliberately non-functional."""

from __future__ import annotations

from typing import Any

from app.lockdown.base import (
    LockdownCapabilities,
    LockdownClientUnavailable,
    LockdownProvider,
)


class DesktopLockdownProvider(LockdownProvider):
    """Placeholder for a signed desktop client that does not exist yet.

    It reports no capabilities and refuses verification. Both are deliberate:

    * returning the capabilities a future client *would* have would put
      "blocks other applications" in front of a teacher today;
    * accepting any attestation would mean a web page could claim to be the
      desktop client and unlock LOCKDOWN mode from inside the browser.

    When a real client is built it attaches here: a signed build, an attestation
    the server verifies against a public key, and a capability set that matches
    what the client actually enforces on each platform.
    """

    name = "desktop"

    def capabilities(self) -> LockdownCapabilities:
        return LockdownCapabilities(
            name=self.name,
            notes=(
                "Not implemented. No desktop lockdown client exists, so no "
                "OS-level guarantee is available from this platform.",
            ),
        )

    def verify_client(self, attestation: dict[str, Any]) -> None:
        raise LockdownClientUnavailable(
            "LOCKDOWN mode requires a desktop lockdown client, which is not "
            "available. This exam cannot be started in LOCKDOWN mode."
        )
