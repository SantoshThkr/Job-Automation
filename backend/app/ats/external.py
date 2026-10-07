"""External (company website) applications: detect the ATS and delegate to its adapter."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from playwright.async_api import Page

from app.ats.registry import detect_ats
from app.browser.engine import BrowserEngine
from app.engine.context import ApplyContext
from app.portals.types import ApplyOutcome, ApplyResult

log = logging.getLogger(__name__)


async def apply_external(engine: BrowserEngine, page: Page, ctx: ApplyContext) -> ApplyResult:
    await engine.wait_for_load(page=page)
    cls = await detect_ats(page)
    if cls is None:
        # single-page career sites render late; give them a moment before giving up
        await page.wait_for_timeout(2500)
        cls = await detect_ats(page)
    if cls is None:
        domain = urlparse(page.url).netloc or page.url
        shot = await engine.screenshot("unknown-ats", page=page)
        await ctx.event("unknown_ats", f"Unknown application platform at {domain}", level="warning", url=page.url)
        return ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED,
                           f"Unknown application platform ({domain}) - please apply manually",
                           external_url=page.url, ats="unknown", screenshot=shot)
    adapter = cls(engine)
    await ctx.event("ats_detected", f"Detected {adapter.display_name}", ats=adapter.name, url=page.url)
    previous = engine.page
    engine.use_page(page)
    try:
        result = await adapter.apply(page, ctx)
    finally:
        if not previous.is_closed():
            engine.use_page(previous)
    result.ats = result.ats or adapter.name
    result.external_url = result.external_url or (page.url if not page.is_closed() else None)
    return result
