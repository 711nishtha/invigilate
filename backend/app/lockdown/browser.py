"""The browser provider: cooperative monitoring, honestly labelled."""

from __future__ import annotations

from typing import Any

from app.lockdown.base import LockdownCapabilities, LockdownProvider


class BrowserLockdownProvider(LockdownProvider):
    """What a normal web page can observe, and nothing more.

    Every OS-level capability stays False. That is the point of this class: it
    is the honest inventory of browser-based proctoring, and the teacher-facing
    UI renders it verbatim rather than paraphrasing it upward.
    """

    name = "browser"

    def capabilities(self) -> LockdownCapabilities:
        return LockdownCapabilities(
            name=self.name,
            can_request_fullscreen=True,
            can_observe_focus=True,
            can_observe_camera=True,
            can_observe_screen_share=True,
            notes=(
                "Fullscreen and focus monitoring are cooperative: a student can "
                "leave, and the platform records that they did.",
                "A browser cannot see other applications, other devices, or the "
                "room beyond the camera's field of view.",
                "Screen sharing shows one surface the student chose to share. It "
                "does not cover a second monitor or a second machine.",
            ),
        )

    def verify_client(self, attestation: dict[str, Any]) -> None:
        """No-op: there is no client to verify, and none is claimed."""
        return None
