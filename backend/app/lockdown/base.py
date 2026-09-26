"""Lockdown provider abstraction.

Read this before adding anything here, because the temptation in this corner of
the codebase is to write something that *looks* like OS lockdown.

**A web page cannot lock down an operating system.** It cannot:

* stop a second application from running, or see that one is running;
* stop a screenshot, a phone camera pointed at the screen, or a second monitor;
* see another device on the desk, or another person in the room;
* prevent a virtual machine, a remote desktop session, or a second computer;
* reliably detect a browser extension, a modified browser, or a headless one;
* keep running once the student closes the tab.

Everything the browser provider reports is a *cooperative* signal: it holds for
a student who is not actively working around it, and it tells you nothing about
one who is. That is worth having — most integrity problems are opportunistic —
but it is not lockdown, and this platform does not call it that.

Real OS-level lockdown needs a signed desktop application with system
privileges. ``DesktopLockdownProvider`` is the seam where one would attach.
**No such client exists yet**, so it refuses every call rather than returning
plausible values, and the server refuses to start a LOCKDOWN attempt. A quiz set
to LOCKDOWN is never silently run as PROCTORED; see
``app/services/precheck_service.py``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class LockdownError(Exception):
    """Base class for lockdown failures."""


class LockdownClientUnavailable(LockdownError):
    """No lockdown client is attached, so the exam cannot run in this mode.

    Raised instead of degrading to browser monitoring. An exam configured for
    OS-level lockdown that quietly runs without it is worse than one that
    refuses to start: the teacher believes in a guarantee they do not have.
    """


@dataclass(frozen=True)
class LockdownCapabilities:
    """What a provider can actually enforce, stated plainly.

    Every field defaults to False. A provider opts in to a claim by setting it,
    so a new provider cannot accidentally inherit a guarantee it does not
    deliver.
    """

    name: str
    # Browser-level, cooperative.
    can_request_fullscreen: bool = False
    can_observe_focus: bool = False
    can_observe_camera: bool = False
    can_observe_screen_share: bool = False
    # OS-level. A browser provider must leave all of these False.
    can_block_other_applications: bool = False
    can_block_screenshots: bool = False
    can_detect_virtual_machine: bool = False
    can_detect_second_display: bool = False
    can_enforce_process_allowlist: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def provides_os_lockdown(self) -> bool:
        """True only for a provider that genuinely controls the machine."""
        return (
            self.can_block_other_applications
            and self.can_enforce_process_allowlist
            and self.can_detect_second_display
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "provides_os_lockdown": self.provides_os_lockdown,
            "can_request_fullscreen": self.can_request_fullscreen,
            "can_observe_focus": self.can_observe_focus,
            "can_observe_camera": self.can_observe_camera,
            "can_observe_screen_share": self.can_observe_screen_share,
            "can_block_other_applications": self.can_block_other_applications,
            "can_block_screenshots": self.can_block_screenshots,
            "can_detect_virtual_machine": self.can_detect_virtual_machine,
            "can_detect_second_display": self.can_detect_second_display,
            "can_enforce_process_allowlist": self.can_enforce_process_allowlist,
            "notes": list(self.notes),
        }


class LockdownProvider(ABC):
    name: str = "abstract"

    @abstractmethod
    def capabilities(self) -> LockdownCapabilities:
        """What this provider can enforce. Must not overstate."""

    @abstractmethod
    def verify_client(self, attestation: dict[str, Any]) -> None:
        """Check that a genuine client is present.

        Raises ``LockdownClientUnavailable`` when it is not. Implementations
        must not accept a self-reported "I am the desktop client" flag from a
        web page: that is exactly the claim an attacker would make, and a
        browser cannot prove it. Only an attestation the server can verify
        cryptographically counts.
        """
