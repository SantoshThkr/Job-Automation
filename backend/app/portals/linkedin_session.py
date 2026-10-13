"""LinkedIn sign-in state, decided from several independent signals (never one URL or one CSS class).

Signals, strongest first:
* the authenticated session cookie (``li_at``) in the browser profile;
* security-verification pages (``/checkpoint/challenge``, PIN / CAPTCHA / "quick security check") - these
  are never reported as signed in, even when a cookie exists;
* the page's URL class: sign-in pages (``/login``, ``/uas/login``, ``/flagship-web/login``, ``/authwall``...),
  authenticated pages (``/feed``, ``/mynetwork``, ``/messaging``...), post-login prompts;
* the page's structure: a visible password box, the guest "Join now / Sign in" header, the member
  navigation (links to My Network / Messaging / Notifications, the "Me" menu, legacy ``.global-nav__me``);
* when the cookie exists but the open tab is ambiguous (e.g. LinkedIn left you on a sign-in URL after
  verification), a background tab confirms that ``/feed/`` opens as a member.

Every tab of the browser is inspected, so a login completed in another tab or after redirects is found.
Nothing here enters credentials or touches CAPTCHA / OTP: those remain manual.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import urlparse

from playwright.async_api import BrowserContext, Error as PlaywrightError, Page

log = logging.getLogger(__name__)

SESSION_COOKIE = "li_at"
REMEMBERED_COOKIES = {"li_rm", "liap"}  # set for members; without li_at they mean the session expired


class LinkedInState(StrEnum):
    AUTHENTICATED = "AUTHENTICATED"
    VERIFICATION = "VERIFICATION"  # security check / OTP / CAPTCHA in progress
    LOGIN_PAGE = "LOGIN_PAGE"  # the sign-in form
    EXPIRED = "EXPIRED"  # you were signed in before, the session is gone
    LOGGED_OUT = "LOGGED_OUT"  # guest pages (Join now / Sign in)
    UNKNOWN = "UNKNOWN"


_LOGIN_URL = re.compile(r"/(?:uas/)?login(?:[/?#]|$)|/flagship-web/login|/checkpoint/lg/login(?:-submit)?(?:[/?#]|$)|"
                        r"/signup|/authwall|/uas/request-password-reset|/reg/join|/start/join", re.I)
_VERIFY_URL = re.compile(r"/checkpoint/(?:challenge|challengesV2|pin|rp)|/uas/consumer-\w*-challenge|"
                         r"two-step-verification|/uas/two-step|/checkpoint/(?!lg/login)", re.I)
_AUTH_URL = re.compile(r"/feed(?:[/?#]|$)|/mynetwork|/messaging|/notifications|/in/[^/?#]+|/my-items|/search/results|"
                       r"/jobs/(?:collections|view|search|tracker)|/premium|/company/[^/]+/admin", re.I)
_POST_LOGIN_URL = re.compile(r"/check/(?:add-phone|manage-account|email-confirmation)|/onboarding|/psettings|"
                             r"/start/(?!join)", re.I)

_PAGE_JS = r"""
() => {
  const T = (e) => (e.innerText || e.textContent || '').replace(/\s+/g, ' ').trim();
  const vis = (e) => !!e && e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden';
  const q = (s) => Array.from(document.querySelectorAll(s)).filter(vis);
  const chrome = q('header, nav, #global-nav, [role="navigation"], [role="banner"]');
  const inChrome = (s) => chrome.some(c => c.querySelector(s) && vis(c.querySelector(s)));
  const chromeText = chrome.map(T).join(' | ');
  const body = (document.body && document.body.innerText || '').slice(0, 4000);
  const navLinks = ['/mynetwork', '/messaging', '/notifications', '/feed', '/jobs']
      .filter(h => q('a[href*="' + h + '"]').some(a => chrome.some(c => c.contains(a)))).length;
  const meMenu = q('.global-nav__me, img.global-nav__me-photo, [data-control-name="nav.settings"], button[aria-label*="Me" i], ' +
                   'button[aria-label*="profile" i], img[alt*="photo of" i], img[alt*="profile" i]')
      .some(e => chrome.some(c => c.contains(e))) || /(^|\|)\s*Me\b/.test(chromeText);
  const guest = q('a.nav__button-secondary, a[data-tracking-control-name*="guest" i][href*="login"], a[href*="/signup"], ' +
                  'a[data-tracking-control-name*="join" i]').length > 0
      || (/\bJoin now\b/.test(chromeText) && /\bSign in\b/.test(chromeText));
  const captcha = q('iframe[src*="arkoselabs"], iframe[src*="funcaptcha"], iframe[src*="recaptcha"], iframe[src*="hcaptcha"], ' +
                    'iframe[title*="challenge" i], #captcha-internal, input[name="pin"], input[autocomplete="one-time-code"]').length > 0;
  return {
    title: document.title || '',
    password: q('input[type="password"]').length > 0,
    email: q('input[type="email"], input#username, input[name="session_key"], input[autocomplete="username"]').length > 0,
    signInHeading: q('h1, h2').some(h => /^\s*(sign in|log in|welcome back)\b/i.test(T(h))),
    guest, navLinks, meMenu, captcha,
    legacyNav: q('.global-nav__me, img.global-nav__me-photo, nav.global-nav .global-nav__primary-items').length > 0,
    challengeText: /let'?s do a quick security check|security verification|verify (?:that )?(?:it'?s|you'?re) you|enter the (?:code|pin)|verification code|we(?:'ve)? sent (?:a|the) (?:code|pin)|two-step verification|are you a robot|confirm (?:it'?s|that it'?s) you/i.test(body),
    expiredText: /session (?:has )?expired|sign in again|welcome back/i.test(body),
  };
}
"""


@dataclass
class PageVerdict:
    state: LinkedInState
    url: str
    signals: list[str] = field(default_factory=list)


@dataclass
class LoginCheck:
    state: LinkedInState
    url: str  # the page the decision is based on (query string removed)
    signals: list[str] = field(default_factory=list)
    session_cookie: bool = False
    pages: list[PageVerdict] = field(default_factory=list)

    @property
    def authenticated(self) -> bool:
        return self.state == LinkedInState.AUTHENTICATED

    def describe(self) -> str:
        where = self.url or "no LinkedIn page open"
        cookie = "authenticated session cookie present" if self.session_cookie else \
            "no authenticated session cookie (li_at)"
        return f"{self.state.value} at {where}; {cookie}; signals: {', '.join(self.signals) or 'none'}"


def clean_url(url: str) -> str:
    p = urlparse(url or "")
    return f"{p.netloc}{p.path}" if p.netloc else (url or "")


def classify_page(url: str, dom: dict | None, *, session_cookie: bool, remembered: bool) -> PageVerdict:
    """Pure decision for one tab (unit-tested without a browser)."""
    dom = dom or {}
    path = urlparse(url or "").path or url or ""
    sig: list[str] = []
    login_url = bool(_LOGIN_URL.search(path))
    verify_url = bool(_VERIFY_URL.search(path))
    auth_url = bool(_AUTH_URL.search(path))
    post_login = bool(_POST_LOGIN_URL.search(path))
    password = bool(dom.get("password"))
    member_nav = bool(dom.get("legacyNav")) or (bool(dom.get("meMenu")) and int(dom.get("navLinks") or 0) >= 2) \
        or int(dom.get("navLinks") or 0) >= 4
    if verify_url:
        sig.append("security-verification URL")
    if dom.get("captcha"):
        sig.append("CAPTCHA / code input on the page")
    if dom.get("challengeText"):
        sig.append("security-check text")
    if login_url:
        sig.append("sign-in URL")
    if password:
        sig.append("password field visible")
    if dom.get("guest"):
        sig.append("guest header (Join now / Sign in)")
    if member_nav:
        sig.append("member navigation (My Network / Messaging / Me)")
    if auth_url:
        sig.append("members-only URL")
    if post_login:
        sig.append("post-sign-in prompt URL")

    # 1. a security check in progress is never "signed in"
    if verify_url or (dom.get("captcha") and not member_nav) or (dom.get("challengeText") and not member_nav and
                                                                 not auth_url):
        return PageVerdict(LinkedInState.VERIFICATION, url, sig)
    # 2. member pages: the session cookie plus a members-only page / navigation / post-login prompt
    if session_cookie and not password and (member_nav or auth_url or post_login):
        return PageVerdict(LinkedInState.AUTHENTICATED, url, sig)
    # 3. without the cookie (e.g. LinkedIn renamed it) the page itself must clearly be a member page
    if not session_cookie and member_nav and not password and not dom.get("guest") and not login_url:
        return PageVerdict(LinkedInState.AUTHENTICATED, url, sig)
    # 4. sign-in forms and guest pages
    if password or login_url or dom.get("signInHeading"):
        # LinkedIn adds session_redirect to every logged-out redirect, so only a remembered-member cookie or the
        # page itself ("session expired", "welcome back") says a previous session expired
        expired = remembered or (dom.get("expiredText") and not session_cookie)
        return PageVerdict(LinkedInState.EXPIRED if expired and not session_cookie else LinkedInState.LOGIN_PAGE, url, sig)
    if dom.get("guest"):
        return PageVerdict(LinkedInState.EXPIRED if remembered else LinkedInState.LOGGED_OUT, url, sig)
    return PageVerdict(LinkedInState.UNKNOWN, url, sig)


_PRIORITY = [LinkedInState.AUTHENTICATED, LinkedInState.VERIFICATION, LinkedInState.LOGIN_PAGE, LinkedInState.EXPIRED,
             LinkedInState.LOGGED_OUT, LinkedInState.UNKNOWN]


class LinkedInSession:
    """Inspects every LinkedIn tab of a browser context (and the profile's cookies)."""

    def __init__(self, context: BrowserContext, base_url: str, *, confirm_interval_s: float = 15) -> None:
        self.context = context
        self.base_url = base_url.rstrip("/")
        self.host = urlparse(self.base_url).netloc
        self.base_path = urlparse(self.base_url).path.rstrip("/")
        self.confirm_interval_s = confirm_interval_s
        self._last_confirm = -1e9

    def _ours(self, url: str) -> bool:
        p = urlparse(url or "")
        if not p.netloc:
            return False
        if self.host.endswith("linkedin.com"):
            return p.netloc.endswith("linkedin.com")
        return p.netloc == self.host and p.path.startswith(self.base_path)

    async def cookies(self) -> tuple[bool, bool]:
        """(authenticated session cookie present, remembered-member cookie present)."""
        try:
            jar = await self.context.cookies([self.base_url + "/"])
        except PlaywrightError:
            return False, False
        now = time.time()
        live = [c for c in jar if c.get("value") and (c.get("expires", -1) in (-1, None) or c["expires"] > now)]
        names = {c["name"] for c in live}
        return SESSION_COOKIE in names, bool(names & REMEMBERED_COOKIES)

    async def _dom(self, page: Page) -> dict | None:
        try:
            return await asyncio.wait_for(page.evaluate(_PAGE_JS), 5)
        except (PlaywrightError, asyncio.TimeoutError):
            return None  # navigating right now

    async def check(self, *, confirm: bool = True, pages: list[Page] | None = None) -> LoginCheck:
        cookie, remembered = await self.cookies()
        verdicts: list[PageVerdict] = []
        for page in pages if pages is not None else list(self.context.pages):
            if page.is_closed() or not self._ours(page.url):
                continue
            verdicts.append(classify_page(page.url, await self._dom(page), session_cookie=cookie,
                                          remembered=remembered))
        best = min(verdicts, key=lambda v: _PRIORITY.index(v.state)) if verdicts else None
        if best is not None and best.state == LinkedInState.AUTHENTICATED:
            return self._result(best, cookie, verdicts)
        # the cookie exists but the open tab is ambiguous (e.g. still on a sign-in URL after verification):
        # confirm in a background tab that a members-only page opens
        verifying = best is not None and best.state == LinkedInState.VERIFICATION
        if cookie and confirm and not verifying and time.monotonic() - self._last_confirm >= self.confirm_interval_s:
            self._last_confirm = time.monotonic()
            confirmed = await self.confirm_in_background()
            if confirmed is not None:
                verdicts.append(confirmed)
                if confirmed.state == LinkedInState.AUTHENTICATED:
                    return self._result(confirmed, cookie, verdicts)
        if best is None:
            state = LinkedInState.EXPIRED if remembered and not cookie else LinkedInState.UNKNOWN
            return LoginCheck(state, "", ["no LinkedIn page open"], cookie, verdicts)
        return self._result(best, cookie, verdicts)

    def _result(self, v: PageVerdict, cookie: bool, verdicts: list[PageVerdict]) -> LoginCheck:
        signals = list(v.signals) + (["authenticated session cookie"] if cookie else [])
        return LoginCheck(v.state, clean_url(v.url), signals, cookie, verdicts)

    async def confirm_in_background(self) -> PageVerdict | None:
        """Open /feed/ in a separate tab (never the one you are using), classify it, close it."""
        page = None
        try:
            page = await self.context.new_page()
            await page.goto(f"{self.base_url}/feed/", wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(2500)  # client-side redirects / rendering
            cookie, remembered = await self.cookies()
            verdict = classify_page(page.url, await self._dom(page), session_cookie=cookie, remembered=remembered)
            verdict.signals.append("checked /feed/ in a background tab")
            return verdict
        except PlaywrightError as exc:
            log.debug("background LinkedIn check failed: %s", exc)
            return None
        finally:
            if page is not None:
                try:
                    await page.close()
                except PlaywrightError:
                    pass


def guidance(check: LoginCheck, history: list[str]) -> str:
    """What to do next, for the state the browser was last seen in."""
    went_through_verification = LinkedInState.VERIFICATION.value in history
    if check.state == LinkedInState.VERIFICATION:
        return ("LinkedIn's security check (code / CAPTCHA / verification) was still open. Complete it in the "
                "browser window and run the command again.")
    if check.state in (LinkedInState.LOGIN_PAGE, LinkedInState.EXPIRED):
        if went_through_verification:
            return ("After the security check LinkedIn returned to its sign-in page and did not create a session "
                    "(no li_at cookie). Sign in again on that page and finish any further check. If LinkedIn keeps "
                    "looping back to sign-in, it is refusing to finish sign-in in this browser: set "
                    "PJA_BROWSER_CHANNEL=chrome in .env (uses your installed Google Chrome) and try again.")
        if check.state == LinkedInState.EXPIRED:
            return "Your earlier LinkedIn session has expired: sign in again in the browser window."
        return "The LinkedIn sign-in form was still showing: enter your email and password in the browser window."
    if check.state == LinkedInState.LOGGED_OUT:
        return "Only LinkedIn's guest pages were open: click 'Sign in' in the browser window and sign in."
    if check.state == LinkedInState.UNKNOWN:
        return "No recognisable LinkedIn page was open: go to linkedin.com/login in the browser window and sign in."
    return ""
