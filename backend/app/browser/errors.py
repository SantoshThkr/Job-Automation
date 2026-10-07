"""Browser/automation exceptions."""

from __future__ import annotations


class StopRequested(Exception):
    """The user pressed Stop; unwind quickly and cleanly."""


class BrowserError(Exception):
    pass


class ElementNotFound(BrowserError):
    pass


class NavigationFailed(BrowserError):
    pass


class BrowserCrashed(BrowserError):
    pass


class ManualActionRequired(Exception):
    """Automation cannot continue without the human (login, CAPTCHA, OTP, unknown form...)."""

    def __init__(self, reason: str, kind: str = "manual") -> None:
        super().__init__(reason)
        self.reason = reason
        self.kind = kind  # login|captcha|otp|unknown_ats|form|manual


class LoginRequired(ManualActionRequired):
    def __init__(self, portal: str, reason: str = "") -> None:
        super().__init__(reason or f"Login required for {portal}", kind="login")
        self.portal = portal
