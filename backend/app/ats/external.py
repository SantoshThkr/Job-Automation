"""External (company website) applications.

Known application systems (Greenhouse, Lever, Workday, SmartRecruiters) use their specialised adapter
first. Everything else - and any known system whose adapter gets structurally stuck - is handled by the
generic navigator, which reads the page and adapts instead of giving up on an unfamiliar layout.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from playwright.async_api import Page

from app.ats.registry import detect_ats
from app.browser.engine import BrowserEngine
from app.config import get_settings
from app.engine.context import ApplyContext
from app.portals.types import ApplyOutcome, ApplyResult

log = logging.getLogger(__name__)


async def apply_external(engine: BrowserEngine, page: Page, ctx: ApplyContext) -> ApplyResult:
    from app.navigator.agent import navigate_application  # local import: navigator uses the ATS registry too

    await engine.wait_for_load(page=page)
    cls = await detect_ats(page)
    if cls is None:
        # single-page career sites render late; give them a moment before deciding
        await page.wait_for_timeout(2500)
        cls = await detect_ats(page)
    domain = urlparse(page.url).netloc or page.url
    navigator_on = get_settings().navigator_enabled
    if cls is None:
        if not navigator_on:
            shot = await engine.screenshot("unknown-ats", page=page)
            await ctx.event("unknown_ats", f"Unknown application platform at {domain}", level="warning", url=page.url)
            return ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED,
                               f"Unknown application platform ({domain}) - please apply manually",
                               external_url=page.url, ats="unknown", screenshot=shot)
        await ctx.event("company_site", f"Company application site {domain}: the navigator takes over", url=page.url)
        return await _with_page(engine, page, lambda: navigate_application(engine, page, ctx))

    adapter = cls(engine)
    await ctx.event("ats_detected", f"Detected {adapter.display_name}", ats=adapter.name, url=page.url)
    ended_on: dict[str, Page] = {}

    async def run_adapter() -> ApplyResult:
        try:
            return await adapter.apply(page, ctx)
        finally:
            ended_on["page"] = engine.page  # the tab the adapter worked in (before the caller's tab is restored)

    result = await _with_page(engine, page, run_adapter)
    if result.handoff and navigator_on and not page.is_closed():
        await ctx.event("handoff", f"{adapter.display_name} adapter stuck ({result.message}); the navigator continues "
                                   "from the same page", level="warning")
        current = ended_on.get("page") or page
        if current.is_closed():
            current = page
        result = await _with_page(engine, current,
                                  lambda: navigate_application(engine, current, ctx, ats_name=adapter.name))
    result.ats = result.ats or adapter.name
    result.external_url = result.external_url or (page.url if not page.is_closed() else None)
    return result


async def _with_page(engine: BrowserEngine, page: Page, fn) -> ApplyResult:
    previous = engine.page
    engine.use_page(page)
    try:
        return await fn()
    finally:
        if not previous.is_closed():
            engine.use_page(previous)
