"""Desktop notifications (macOS Notification Center via osascript) when the agent needs you."""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import time

from app.config import get_settings

log = logging.getLogger(__name__)

_last: dict[str, float] = {}


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def notify(title: str, message: str, *, key: str = "", min_interval_s: float = 20) -> bool:
    """Best effort, non-blocking. Identical notifications are rate-limited."""
    if not get_settings().notify or sys.platform != "darwin" or not shutil.which("osascript"):
        return False
    k = key or f"{title}|{message}"
    now = time.monotonic()
    if now - _last.get(k, -1e9) < min_interval_s:
        return False
    _last[k] = now
    script = (f'display notification "{_escape(message)[:220]}" with title "Job Assistant" '
              f'subtitle "{_escape(title)[:80]}" sound name "Glass"')
    try:
        subprocess.Popen(["osascript", "-e", script], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except OSError as exc:
        log.debug("notification failed: %s", exc)
        return False
