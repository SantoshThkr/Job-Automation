"""Browser engine: a thin, resilient layer over Playwright used by every portal / ATS adapter.

Features: persistent session (cookies survive restarts, so you log in once), robust multi-strategy
element lookup, retries on navigation timeouts, infinite-scroll helper, page-change detection,
pause/resume/stop checkpoints before each action, crash recovery and failure screenshots.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.async_api import (
    BrowserContext,
    Frame,
    Locator,
    Page,
    Playwright,
    async_playwright,
)
from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeout,
)

from app.security import is_web_url, scrub_html
from app.browser.errors import BrowserCrashed, ElementNotFound, NavigationFailed
from app.browser.selectors import Target
from app.config import Settings, get_settings

log = logging.getLogger(__name__)

Checkpoint = Callable[[], Awaitable[None]]
Root = Page | Frame | Locator

# Full-page / pop-up challenges: nothing else can happen until a human solves them.
CHALLENGE_SELECTORS = [
    "iframe[src*='recaptcha/api2/bframe']",
    "iframe[src*='recaptcha/enterprise/bframe']",
    "iframe[src*='hcaptcha.com'][src*='challenge']",
    "iframe[title*='hCaptcha challenge' i]", "iframe[title*='challenge' i][src*='hcaptcha']",
    "iframe[title*='challenge' i]:not([title*='reCAPTCHA' i])",
    "iframe[src*='challenges.cloudflare.com']:not([src*='turnstile'])",
    "iframe[src*='arkoselabs']",
    "#captcha",
    "#captcha-internal",
    "[data-testid='captcha']",
]
# Inline "I'm not a robot" widgets inside a form (solved by the human right before submitting).
# The invisible reCAPTCHA v3 badge (size=invisible) is not a challenge and is ignored.
CAPTCHA_WIDGET_SELECTORS = [
    "iframe[title*='reCAPTCHA' i]:not([style*='hidden']):not([src*='size=invisible'])",
    "iframe[src*='hcaptcha.com'][src*='checkbox']",
    "iframe[src*='challenges.cloudflare.com'][src*='turnstile']",
    ".g-recaptcha:not([data-size='invisible'])",
    ".h-captcha",
    ".cf-turnstile",
    # a code / sum to type ("What is 3 + 4?", an image of letters): still a CAPTCHA, still yours
    "input[name*='captcha' i]:not([type='hidden']):not([name*='response' i])",
    "input[id*='captcha' i]:not([type='hidden']):not([id*='response' i])",
]
CAPTCHA_SELECTORS = CHALLENGE_SELECTORS + CAPTCHA_WIDGET_SELECTORS
# where a solved inline CAPTCHA leaves its proof (a token the widget writes, or the answer you typed)
_CAPTCHA_SOLVED_JS = """
() => [...document.querySelectorAll('textarea[name="g-recaptcha-response"], textarea[name="h-captcha-response"], ' +
        'input[name="cf-turnstile-response"], input[name*="captcha" i]:not([type="hidden"]), ' +
        'input[id*="captcha" i]:not([type="hidden"]), .g-recaptcha input[type="checkbox"], .h-captcha input[type="checkbox"]')]
      .some(e => e.type === 'checkbox' ? e.checked : (e.value || '').trim().length > 0)
"""
OTP_SELECTORS = [
    "input[autocomplete='one-time-code']",
    "input[name*='otp' i]",
    "input[id*='otp' i]",
    "input[name*='verification' i][name*='code' i]",
    "input[name='pin'][maxlength]",
    # one box per digit, named only for screen readers ("Enter verification code digit 1 of six.")
    "input[aria-label*='verification code' i]",
    "input[aria-label*='one-time' i]",
    "input[aria-label*='security code' i]",
    "input[aria-label*='digit 1' i][maxlength='1']",
]
OTP_TEXT = re.compile(r"enter\s+(?:the\s+)?otp|one[\s-]time\s+(?:password|pass\s*code|code)|verification\s+code\s+(?:sent|to)|"
                      r"we\s+(?:sent|have\s+sent|(?:have\s+)?e-?mailed|texted)\s+(?:you\s+)?(?:a|an|the)\s+(?:\w+\s+)?(?:code|otp)|"
                      r"enter\s+(?:the\s+)?(?:6|4|six|four)[\s-]digit|enter\s+(?:the\s+|your\s+)?(?:verification|security|"
                      r"confirmation)\s+code", re.I)
CAPTCHA_TEXT = re.compile(r"verify\s+(?:that\s+)?you\s+are\s+(?:a\s+)?human|"
                          r"security\s+check|unusual\s+(?:activity|traffic)|are\s+you\s+a\s+robot|"
                          r"let'?s\s+do\s+a\s+quick\s+security\s+check", re.I)
WIDGET_TEXT = re.compile(r"i'?m\s+not\s+a\s+robot", re.I)  # the label of an inline CAPTCHA checkbox


CAPTURE_TIMEOUT_S = 8.0  # saving a failed page's source: evidence, never worth waiting on

# Advertising and marketing trackers that LinkedIn and other portals embed as cross-site iframes. Chrome's site isolation
# runs every cross-site frame in a renderer process of its own: measured on 2026-10-15, one LinkedIn tab = 1 page +
# 7-8 such frames (DoubleClick x3, Adobe demdex, ns1p, ...) = 10 renderers, ~2.7 GB RSS. They are resolved to "not
# found" inside this browser only (no request interception, no cost per request). Never listed: CAPTCHA, bot
# protection, sign-in and payment services (reCAPTCHA / google.com, gstatic, hCaptcha, Cloudflare, PerimeterX /
# protechts, Arkose, LinkedIn's own hosts) - they are security controls, not overhead.
TRACKER_HOSTS = ("*.doubleclick.net", "doubleclick.net", "*.demdex.net", "*.ns1p.net", "*.googlesyndication.com",
                 "*.googleadservices.com", "*.adnxs.com", "*.adsrvr.org", "*.criteo.com", "*.criteo.net",
                 "*.taboola.com", "*.outbrain.com", "*.scorecardresearch.com", "*.moatads.com")


def tracker_block_arg() -> str:
    return "--host-resolver-rules=" + ", ".join(f"MAP {host} ~NOTFOUND" for host in TRACKER_HOSTS)


class BrowserEngine:
    def __init__(self, settings: Settings | None = None, *, headless: bool | None = None,
                 user_data_dir: Path | None = None, checkpoint: Checkpoint | None = None,
                 screenshot_dir: Path | None = None, human_delays: bool = True,
                 persistent: bool = True) -> None:
        self.settings = settings or get_settings()
        self.headless = self.settings.browser_headless if headless is None else headless
        self.user_data_dir = user_data_dir or self.settings.browser_profile_path
        self.screenshot_dir = screenshot_dir or self.settings.screenshots_path
        self._checkpoint = checkpoint
        self.human_delays = human_delays
        self.persistent = persistent
        self.nav_timeout = self.settings.nav_timeout_ms
        self.action_timeout = self.settings.action_timeout_ms
        self._pw: Playwright | None = None
        self._browser = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._context_closed = False
        self._crashed_pages: set[int] = set()
        self._watched: set[int] = set()
        self.restarts = 0

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self._context is not None and not self._context_closed:
            return
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        if self._pw is None:
            self._pw = await async_playwright().start()
        launch_args = ["--disable-blink-features=AutomationControlled", "--no-first-run",
                       "--no-default-browser-check"]
        if self.settings.browser_block_trackers:
            launch_args.append(tracker_block_arg())
        if self.persistent and self.settings.browser_disk_cache_mb > 0:
            # the profile keeps your sessions; its HTTP cache only needs to be small (it had grown to 650 MB)
            launch_args.append(f"--disk-cache-size={self.settings.browser_disk_cache_mb * 1048576}")
        common: dict[str, Any] = {
            "headless": self.headless,
            "slow_mo": self.settings.browser_slow_mo_ms or None,
            "args": launch_args,
        }
        if self.settings.browser_channel:
            common["channel"] = self.settings.browser_channel
        ctx_opts: dict[str, Any] = {"viewport": {"width": 1366, "height": 900}, "locale": "en-IN",
                                    "timezone_id": "Asia/Kolkata"}
        if self.persistent:
            self.user_data_dir.mkdir(parents=True, exist_ok=True)
            self._context = await self._pw.chromium.launch_persistent_context(
                str(self.user_data_dir), **common, **ctx_opts)
        else:
            self._browser = await self._pw.chromium.launch(**common)
            self._context = await self._browser.new_context(**ctx_opts)
        self._context_closed = False
        self._context.on("close", lambda _c: self._mark_closed())
        self._context.set_default_timeout(self.action_timeout)
        self._context.set_default_navigation_timeout(self.nav_timeout)
        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()
        self._watch_page(self._page)
        log.info("Browser started (headless=%s, persistent=%s)", self.headless, self.persistent)

    def _mark_closed(self) -> None:
        self._context_closed = True

    def _watch_page(self, page: Page) -> None:
        if id(page) in self._watched:
            return  # use_page() runs for every tab switch: the listener is attached once per page
        self._watched.add(id(page))
        page.on("crash", lambda p: self._crashed_pages.add(id(p)))
        page.on("close", lambda p: self._watched.discard(id(p)))

    async def stop(self) -> None:
        try:
            if self._context is not None and not self._context_closed:
                await self._context.close()
        except Exception as exc:  # pragma: no cover - best effort
            log.debug("context close: %s", exc)
        try:
            if self._browser is not None:
                await self._browser.close()
        except Exception:  # pragma: no cover
            pass
        try:
            if self._pw is not None:
                await self._pw.stop()
        except Exception:  # pragma: no cover
            pass
        self._context = None
        self._browser = None
        self._page = None
        self._pw = None
        self._watched.clear()
        self._crashed_pages.clear()
        log.info("Browser stopped")

    @property
    def alive(self) -> bool:
        if self._context is None or self._context_closed:
            return False
        if self._browser is not None and not self._browser.is_connected():
            return False
        return True

    async def ensure_alive(self) -> None:
        """Restart the browser after a crash / manual close; replace crashed pages."""
        if not self.alive:
            log.warning("Browser is not running - restarting")
            self.restarts += 1
            self._context = None
            if self._browser is not None:
                try:
                    await self._browser.close()
                except Exception:
                    pass
                self._browser = None
            await self.start()
            return
        if self._page is None or self._page.is_closed() or id(self._page) in self._crashed_pages:
            log.warning("Page closed or crashed - opening a new tab")
            old = self._page
            self._crashed_pages.discard(id(old))
            assert self._context is not None
            self._page = await self._context.new_page()
            self._watch_page(self._page)
            if old is not None and not old.is_closed():
                try:
                    await old.close()  # a crashed tab is not left behind
                except Exception:  # noqa: BLE001
                    pass

    @property
    def page(self) -> Page:
        if self._page is None:
            raise BrowserCrashed("Browser not started")
        return self._page

    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise BrowserCrashed("Browser not started")
        return self._context

    def use_page(self, page: Page) -> None:
        self._page = page
        self._watch_page(page)

    async def new_page(self) -> Page:
        await self.ensure_alive()
        page = await self.context.new_page()
        self._watch_page(page)
        return page

    async def fresh_page(self) -> Page:
        """A unit-of-work boundary (one application, one job read during discovery): continue in a new tab and
        close the previous one. Chrome keeps one renderer per tab for same-site navigation, and heavy sites
        (LinkedIn) never give its memory back - measured: one reused tab ~2-3 GB after 10 LinkedIn job pages, a fresh
        tab per job ~0.6 GB. The browser, the persistent context and every login session stay as they are; the new
        tab is opened before the old one is closed, so the context always keeps a page."""
        await self.ensure_alive()
        old = self._page
        page = await self.context.new_page()  # (a page's own window.close() cannot close this tab - measured)
        self._watch_page(page)
        self._page = page
        if old is not None and old is not page and not old.is_closed():
            try:
                await old.close()
            except PlaywrightError as exc:
                log.debug("closing the previous tab: %s", exc)
        return page

    # ------------------------------------------------------------ control
    async def checkpoint(self) -> None:
        if self._checkpoint is not None:
            await self._checkpoint()

    async def human_delay(self, min_ms: int | None = None, max_ms: int | None = None) -> None:
        if not self.human_delays:
            return
        lo = self.settings.human_delay_ms_min if min_ms is None else min_ms
        hi = self.settings.human_delay_ms_max if max_ms is None else max_ms
        if hi > 0:
            await asyncio.sleep(random.uniform(lo, max(lo, hi)) / 1000)

    # --------------------------------------------------------- navigation
    async def goto(self, url: str, *, wait_until: str = "domcontentloaded", retries: int = 2,
                   page: Page | None = None) -> Page:
        await self.checkpoint()
        # A link scraped from a job page decides where the automation browser goes: web addresses only (a
        # posting's "apply" link could just as well be file:, chrome:, javascript: or data:).
        if url != "about:blank" and not is_web_url(url):
            log.warning("Refused to open a non-web address: %s", str(url)[:80])
            raise NavigationFailed(f"Refused to open a non-web address ({str(url).split(':', 1)[0][:20]}:...)")
        await self.ensure_alive()
        p = page or self.page
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                await p.goto(url, wait_until=wait_until, timeout=self.nav_timeout)  # type: ignore[arg-type]
                await self.human_delay()
                return p
            except PlaywrightTimeout as exc:
                last = exc
                log.warning("Navigation timeout (%d/%d) %s", attempt + 1, retries + 1, url)
            except PlaywrightError as exc:
                last = exc
                msg = str(exc)
                log.warning("Navigation error (%d/%d) %s: %s", attempt + 1, retries + 1, url, msg.splitlines()[0])
                if "Target page, context or browser has been closed" in msg or "crash" in msg.lower():
                    await self.ensure_alive()
                    p = self.page
            await asyncio.sleep(min(2 ** attempt, 8))
        raise NavigationFailed(f"Could not load {url}: {last}")

    async def wait_for_load(self, state: str = "domcontentloaded", timeout: int | None = None,
                            page: Page | None = None) -> None:
        try:
            await (page or self.page).wait_for_load_state(state, timeout=timeout or self.nav_timeout)  # type: ignore[arg-type]
        except PlaywrightTimeout:
            log.debug("wait_for_load_state(%s) timed out", state)

    async def page_signature(self, page: Page | None = None) -> str:
        p = page or self.page
        try:
            info = await p.evaluate(
                "() => [location.href, document.title, (document.body && document.body.innerText || '').length,"
                " document.querySelectorAll('*').length].join('|')")
        except PlaywrightError:
            info = p.url
        return hashlib.sha1(str(info).encode(), usedforsecurity=False).hexdigest()  # a change fingerprint

    async def wait_for_page_change(self, before: str, *, timeout_ms: int = 10_000, page: Page | None = None,
                                   poll_ms: int = 250) -> bool:
        """Wait until the URL/DOM signature differs from ``before`` (navigation, SPA route, modal...)."""
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            await self.checkpoint()
            try:
                if await self.page_signature(page) != before:
                    return True
            except PlaywrightError:
                return True  # navigated mid-evaluation
            await asyncio.sleep(poll_ms / 1000)
        return False

    # ------------------------------------------------------------ finding
    async def find(self, target: Target, *, root: Root | None = None, timeout_ms: int = 0,
                   visible: bool = True) -> Locator | None:
        """Return the first element matched by any strategy (polling up to ``timeout_ms``)."""
        base = root or self.page
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            for desc, loc in target.strategies(base):
                try:
                    count = await loc.count()
                except PlaywrightError:
                    continue
                for i in range(min(count, 10)):
                    el = loc.nth(i)
                    try:
                        if not visible or await el.is_visible():
                            return el
                    except PlaywrightError:
                        continue
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.2)

    async def find_all(self, target: Target, *, root: Root | None = None, visible: bool = True) -> list[Locator]:
        base = root or self.page
        for _desc, loc in target.strategies(base):
            try:
                count = await loc.count()
            except PlaywrightError:
                continue
            if not count:
                continue
            items = [loc.nth(i) for i in range(count)]
            if visible:
                items = [el for el in items if await _safe_visible(el)]
            if items:
                return items
        return []

    async def exists(self, target: Target, *, root: Root | None = None, timeout_ms: int = 0) -> bool:
        return await self.find(target, root=root, timeout_ms=timeout_ms) is not None

    async def wait_for(self, target: Target, *, root: Root | None = None, timeout_ms: int | None = None) -> Locator:
        el = await self.find(target, root=root, timeout_ms=self.action_timeout if timeout_ms is None else timeout_ms)
        if el is None:
            raise ElementNotFound(f"Element not found: {target.describe()}")
        return el

    async def wait_for_any(self, targets: dict[str, Target], *, root: Root | None = None,
                           timeout_ms: int = 10_000) -> tuple[str | None, Locator | None]:
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            await self.checkpoint()
            for key, t in targets.items():
                el = await self.find(t, root=root)
                if el is not None:
                    return key, el
            if time.monotonic() >= deadline:
                return None, None
            await asyncio.sleep(0.3)

    async def text_of(self, target: Target | Locator, *, root: Root | None = None, default: str = "") -> str:
        el = target if isinstance(target, Locator) else await self.find(target, root=root, visible=False)
        if el is None:
            return default
        try:
            return (await el.inner_text(timeout=3000)).strip()
        except PlaywrightError:
            try:
                return ((await el.text_content(timeout=2000)) or "").strip()
            except PlaywrightError:
                return default

    async def attr_of(self, target: Target | Locator, name: str, *, root: Root | None = None) -> str | None:
        el = target if isinstance(target, Locator) else await self.find(target, root=root, visible=False)
        if el is None:
            return None
        try:
            return await el.get_attribute(name, timeout=2000)
        except PlaywrightError:
            return None

    # ------------------------------------------------------------ actions
    async def _resolve(self, target: Target | Locator, root: Root | None, timeout_ms: int | None) -> Locator:
        if isinstance(target, Locator):
            return target
        return await self.wait_for(target, root=root, timeout_ms=timeout_ms)

    async def click(self, target: Target | Locator, *, root: Root | None = None, timeout_ms: int | None = None,
                    force: bool = False, retries: int = 2) -> None:
        await self.checkpoint()
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                el = await self._resolve(target, root, timeout_ms)
                try:
                    await el.scroll_into_view_if_needed(timeout=3000)
                except PlaywrightError:
                    pass
                await el.click(timeout=timeout_ms or self.action_timeout, force=force)
                await self.human_delay()
                return
            except ElementNotFound:
                raise
            except PlaywrightError as exc:
                last = exc
                log.debug("click retry %d on %s: %s", attempt + 1, _describe(target), str(exc).splitlines()[0])
                if attempt == retries - 1:
                    force = True  # overlays (cookie banners, sticky headers) intercepting clicks
                await asyncio.sleep(0.5)
        raise ElementNotFound(f"Could not click {_describe(target)}: {last}")

    async def type(self, target: Target | Locator, text: str, *, root: Root | None = None, clear: bool = True,
                   delay_ms: int | None = None, timeout_ms: int | None = None) -> None:
        await self.checkpoint()
        el = await self._resolve(target, root, timeout_ms)
        await el.scroll_into_view_if_needed(timeout=3000)
        if clear:
            await el.fill("", timeout=timeout_ms or self.action_timeout)
        delay = delay_ms if delay_ms is not None else (random.randint(20, 60) if self.human_delays else 0)
        await el.press_sequentially(text, delay=delay, timeout=max(self.action_timeout, len(text) * (delay + 30)))
        await self.human_delay(80, 300)

    async def fill(self, target: Target | Locator, text: str, *, root: Root | None = None,
                   timeout_ms: int | None = None) -> None:
        await self.checkpoint()
        el = await self._resolve(target, root, timeout_ms)
        await el.fill(text, timeout=timeout_ms or self.action_timeout)
        await self.human_delay(50, 200)

    async def select(self, target: Target | Locator, *, label: str | None = None, value: str | None = None,
                     root: Root | None = None) -> None:
        await self.checkpoint()
        el = await self._resolve(target, root, None)
        if label is not None:
            await el.select_option(label=label)
        else:
            await el.select_option(value=value)
        await self.human_delay(50, 200)

    async def upload(self, target: Target | Locator, path: str | Path, *, root: Root | None = None) -> None:
        await self.checkpoint()
        el = await self._resolve(target, root, None) if not isinstance(target, Locator) else target
        await el.set_input_files(str(path))
        await self.human_delay(300, 800)

    async def press(self, key: str, *, page: Page | None = None) -> None:
        await (page or self.page).keyboard.press(key)

    async def scroll_by(self, dy: int = 800, *, page: Page | None = None) -> None:
        await self.checkpoint()
        await (page or self.page).mouse.wheel(0, dy)
        await asyncio.sleep(0.3)

    async def scroll_to_load_all(self, item_target: Target, *, max_rounds: int = 15, root: Root | None = None,
                                 container_selector: str | None = None, page: Page | None = None) -> int:
        """Infinite-scroll helper: scroll until the number of items stops growing."""
        p = page or self.page
        stable_rounds, last_count = 0, -1
        for _ in range(max_rounds):
            await self.checkpoint()
            items = await self.find_all(item_target, root=root, visible=False)
            count = len(items)
            if count == last_count:
                stable_rounds += 1
                if stable_rounds >= 2:
                    break
            else:
                stable_rounds = 0
            last_count = count
            if items:
                try:
                    await items[-1].scroll_into_view_if_needed(timeout=2000)
                except PlaywrightError:
                    pass
            if container_selector:
                await p.evaluate("(sel) => { const el = document.querySelector(sel); if (el) el.scrollTop = el.scrollHeight; }",
                                 container_selector)
            else:
                await p.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(0.8 if self.human_delays else 0.3)
        return max(last_count, 0)

    async def expect_popup(self, action: Callable[[], Awaitable[None]], timeout_ms: int = 8000) -> Page | None:
        """Run ``action`` (e.g. a click) and return a newly opened tab, if any."""
        try:
            async with self.context.expect_page(timeout=timeout_ms) as info:
                await action()
            new_page = await info.value
            self._watch_page(new_page)
            await self.wait_for_load(page=new_page)
            return new_page
        except PlaywrightTimeout:
            return None

    # ------------------------------------------------------ diagnostics
    async def screenshot(self, name: str, *, page: Page | None = None, full_page: bool = False) -> str | None:
        p = page or self._page
        if p is None or p.is_closed():
            return None
        safe = re.sub(r"[^A-Za-z0-9_-]+", "-", name)[:80]
        path = self.screenshot_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{safe}.png"
        try:
            await p.screenshot(path=str(path), full_page=full_page, timeout=10_000)
            return str(path)
        except PlaywrightError as exc:
            log.debug("screenshot failed: %s", exc)
            return None

    async def capture_failure(self, name: str, *, page: Page | None = None) -> dict[str, str | None]:
        p = page or self._page
        shot = await self.screenshot(f"fail-{name}", page=p, full_page=True)
        html_path = None
        if p is not None and not p.is_closed():
            try:
                # (bounded: on a page stuck in a navigation that never finishes, content() waits for ever - run #42
                # hung there for 9 minutes after a LinkedIn page timed out, deaf to Stop, with the browser open)
                # (saved for diagnosis: never with a password in it - see scrub_html)
                html = scrub_html(await asyncio.wait_for(p.content(), CAPTURE_TIMEOUT_S))
                html_file = Path(shot).with_suffix(".html") if shot else (
                    self.screenshot_dir / f"{datetime.now():%Y%m%d-%H%M%S}-fail-{name}.html")
                html_file.write_text(html, encoding="utf-8")
                html_path = str(html_file)
            except (PlaywrightError, asyncio.TimeoutError, OSError) as exc:
                log.debug("page source not saved: %s", exc.__class__.__name__)
        return {"screenshot": shot, "html": html_path, "url": p.url if p and not p.is_closed() else None}

    async def captcha_widget_present(self, page: Page | None = None) -> bool:
        """An inline CAPTCHA widget is visible (in the page or in an embedded application iframe)."""
        p = page or self.page
        for frame in [p.main_frame, *[f for f in p.frames if f is not p.main_frame]][:8]:
            for sel in CAPTCHA_WIDGET_SELECTORS:
                try:
                    loc = frame.locator(sel)
                    if await loc.count() and await loc.first.is_visible():
                        return True
                except PlaywrightError:
                    continue
        return False

    async def captcha_widget_solved(self, page: Page | None = None) -> bool:
        """The inline CAPTCHA on the page (or in an embedded form) carries its proof: the widget's token, or the
        answer typed into a code / sum box. Read-only: the assistant never solves or fills one."""
        p = page or self.page
        for frame in [p.main_frame, *[f for f in p.frames if f is not p.main_frame]][:8]:
            try:
                if await frame.evaluate(_CAPTCHA_SOLVED_JS):
                    return True
            except PlaywrightError:
                continue
        return False

    async def detect_blockers(self, page: Page | None = None, *, include_widgets: bool = True) -> list[str]:
        """Detect CAPTCHA / OTP challenges that require a human.

        ``include_widgets=False`` ignores inline CAPTCHA checkboxes inside forms (see captcha_widget_present).
        """
        p = page or self.page
        found: list[str] = []
        for sel in (CAPTCHA_SELECTORS if include_widgets else CHALLENGE_SELECTORS):
            try:
                loc = p.locator(sel)
                if await loc.count() and await loc.first.is_visible():
                    found.append("captcha")
                    break
            except PlaywrightError:
                continue
        for sel in OTP_SELECTORS:
            try:
                loc = p.locator(sel)
                if await loc.count() and await loc.first.is_visible():
                    found.append("otp")
                    break
            except PlaywrightError:
                continue
        try:
            body = await p.evaluate("() => (document.body && document.body.innerText || '').slice(0, 5000)")
        except PlaywrightError:
            body = ""
        if "captcha" not in found and (CAPTCHA_TEXT.search(body or "")
                                       or (include_widgets and WIDGET_TEXT.search(body or ""))):
            found.append("captcha")
        if "otp" not in found and OTP_TEXT.search(body or ""):
            found.append("otp")
        return found

    async def all_text(self, page: Page | None = None, limit: int = 12_000, max_frames: int = 6) -> str:
        """Visible text of the page and its child frames (embedded application forms live in iframes)."""
        p = page or self.page
        parts: list[str] = []
        for frame in [p.main_frame, *[f for f in p.frames if f is not p.main_frame]][:max_frames]:
            try:
                parts.append(await asyncio.wait_for(frame.evaluate(
                    "(n) => (document.body && document.body.innerText || '').slice(0, n)", limit), 4))
            except (PlaywrightError, TimeoutError):
                continue
        return "\n".join(t for t in parts if t)

    async def body_text(self, page: Page | None = None, limit: int = 20_000) -> str:
        try:
            return await (page or self.page).evaluate(
                "(n) => (document.body && document.body.innerText || '').slice(0, n)", limit)
        except PlaywrightError:
            return ""


async def _safe_visible(el: Locator) -> bool:
    try:
        return await el.is_visible()
    except PlaywrightError:
        return False


def _describe(target: Target | Locator) -> str:
    return target.describe() if isinstance(target, Target) else str(target)
