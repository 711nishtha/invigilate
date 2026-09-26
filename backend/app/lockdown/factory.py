"""Lockdown provider selection, keyed by the quiz's proctoring mode."""

from __future__ import annotations

from app.db.models import ProctoringMode
from app.lockdown.base import LockdownProvider


def get_lockdown_provider(mode: ProctoringMode) -> LockdownProvider:
    """The provider a quiz in ``mode`` would need.

    LOCKDOWN returns the desktop provider, which refuses. That refusal is the
    designed behaviour, not a gap to route around: mapping LOCKDOWN to the
    browser provider here is precisely the silent downgrade the platform
    promises not to perform.
    """
    if mode is ProctoringMode.LOCKDOWN:
        from app.lockdown.desktop import DesktopLockdownProvider

        return DesktopLockdownProvider()

    from app.lockdown.browser import BrowserLockdownProvider

    return BrowserLockdownProvider()
