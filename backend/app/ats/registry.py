"""ATS detection and adapter registry."""

from __future__ import annotations

import logging

from playwright.async_api import Error as PlaywrightError, Page

from app.ats.adapters import GreenhouseAdapter, LeverAdapter, SmartRecruitersAdapter, WorkdayAdapter
from app.ats.base import ATSAdapter
from app.browser.engine import BrowserEngine

log = logging.getLogger(__name__)

ATS_ADAPTERS: list[type[ATSAdapter]] = [GreenhouseAdapter, LeverAdapter, WorkdayAdapter, SmartRecruitersAdapter]


async def detect_ats(page: Page) -> type[ATSAdapter] | None:
    """Identify the application platform from the URL, embedded iframes, then DOM signals."""
    url = page.url
    for cls in ATS_ADAPTERS:
        if cls.matches_url(url):
            return cls
    try:
        for frame in page.frames:
            for cls in ATS_ADAPTERS:
                if frame is not page.main_frame and cls.matches_url(frame.url):
                    return cls
    except PlaywrightError:
        pass
    for cls in ATS_ADAPTERS:
        for sel in cls.dom_signals:
            try:
                if await page.locator(sel).count():
                    return cls
            except PlaywrightError:
                continue
    return None


def adapter_for(name: str, engine: BrowserEngine) -> ATSAdapter | None:
    for cls in ATS_ADAPTERS:
        if cls.name == name:
            return cls(engine)
    return None
