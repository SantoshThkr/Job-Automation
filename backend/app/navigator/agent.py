"""ApplicationNavigator: observe -> understand -> decide -> act -> verify, until the application is done.

Works on company career sites and application systems it has never seen. Each step:

1. observe the page (fields, buttons, dialogs, iframes, password boxes, CAPTCHA/OTP, page text);
2. recognise the situation: confirmation, already applied, closed job, challenge, cookie banner or
   popup, sign-in / sign-up wall, application form, job description with an Apply button, or unknown;
3. act with deterministic rules (fill fields from your profile and saved answers, click the best
   Apply / Next / Guest button) and fall back to the local LLM, which may only pick one of the
   enumerated on-page elements;
4. verify the page changed; remember what was tried so it never loops on the same click.

Anything irreversible goes through ``ApplyContext.decide_submit`` (Dry Run / Review / Auto). A job is
APPLIED only with confirmation text that appeared *after* the submit click. When something genuinely
needs you (CAPTCHA, OTP, email verification, sign-in, unanswered required question) it pauses, tells
you, and continues from the same page.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Frame, Page
from playwright.async_api import TimeoutError as PlaywrightTimeout
from rapidfuzz import fuzz

from app.browser.engine import BrowserEngine
from app.browser.forms import FillReport, FormFiller, collect_errors
from app.config import get_settings
from app.site_logins import SiteCredential, normalize_site, same_site
from app.site_logins import lookup as lookup_credential
from app.engine.context import ApplyContext, GateResult, SubmitDecision
from app.engine.questions import ask_and_fill
from app.navigator.classify import (
    ACCOUNT_EXISTS_TEXT,
    CONFIRM_PASSWORD_RX,
    LOGIN_ERROR_TEXT,
    PROMO_DIALOG_RX,
    SEARCH_BUTTON_RX,
    UNAVAILABLE_TEXT,
    VERIFY_EMAIL_TEXT,
    Kind,
    classify_element,
    norm,
    success_evidence,
)
from app.navigator.observe import Element, Snapshot, observe
from app.navigator.planner import PageReasoner
from app.engine.submission import SubmissionState, SubmissionWatcher
from app.portals.types import ApplyOutcome, ApplyResult

log = logging.getLogger(__name__)

_EMAIL_FIELD = re.compile(r"e-?mail|user\s*name|login|user\s*id|account", re.I)
_OPTIN = re.compile(r"\bfollow\b|subscribe|newsletter|marketing|promotional|job\s+alerts?|talent\s+(?:community|network)|"
                    r"(?:send|receive|get)\s+(?:me\s+)?(?:updates|news|offers|emails)|similar\s+(?:jobs|roles)", re.I)
_SIGNUP_TEXT = re.compile(r"create\s+(?:an?\s+|your\s+)?account|register|sign\s*up|new\s+(?:user|candidate)", re.I)


def site_label(url: str) -> str:
    host = urlparse(url or "").netloc.lower()
    return host[4:] if host.startswith("www.") else host


# template addresses on career pages ("send your resume to jobs@company.com") are not real inboxes
_PLACEHOLDER_EMAIL = re.compile(r"@(?:(?:your)?company|example|domain|email|test|sample|yourdomain|mail)\.(?:com|in|org)\b|"
                                r"^(?:name|your(?:name|email)?|user|email)@", re.I)
_APPLICATIONS_PAGE = re.compile(r"application|my[-_]?jobs|dashboard|candidate|profile/jobs|status", re.I)


def application_listed(body: str, title: str) -> str:
    """Candidate portals often land on 'My applications' after submitting: the job listed as submitted."""
    t = re.sub(r"\s+", " ", (title or "").strip())
    if len(t) < 6:
        return ""
    low = re.sub(r"\s+", " ", body or "").lower()
    i = low.find(t.lower())
    while i != -1:
        window = low[i: i + len(t) + 160]
        m = re.search(r"\b(submitted|under\s+review|in\s+review|application\s+received|received|applied)\b", window)
        if m:
            return f"Applications list shows '{t}' as {m.group(1)}"
        i = low.find(t.lower(), i + 1)
    return ""


class Finish(Exception):
    """Internal: stop the loop with a result."""

    def __init__(self, result: ApplyResult) -> None:
        super().__init__(result.message)
        self.result = result


# Sign-in pages of identity providers. An "apply" link sometimes leads to one (a Google Form that requires an
# account, "Apply with Microsoft" ...). They are never treated as a form: run #35 typed the e-mail address into
# Google's "Email or phone" box and pressed Next before asking.
_IDP_HOSTS = re.compile(r"(?:^|\.)(?:accounts\.google\.[a-z.]+|accounts\.youtube\.com|login\.microsoftonline\.com|login\.live\.com|"
                        r"login\.microsoft\.com|appleid\.apple\.com|idmsa\.apple\.com|login\.yahoo\.com|id\.atlassian\.com|"
                        r"accounts\.zoho\.(?:com|in|eu)|login\.salesforce\.com|[\w-]+\.auth0\.com|[\w-]+\.okta\.com|"
                        r"[\w-]+\.onelogin\.com|[\w-]+\.b2clogin\.com)$", re.I)
_IDP_NAMES = [("Google", r"google|youtube"), ("Microsoft", r"microsoft|live\.com|b2clogin"), ("Apple", r"apple"),
              ("Yahoo", r"yahoo"), ("Atlassian", r"atlassian"), ("Zoho", r"zoho"), ("Salesforce", r"salesforce"),
              ("Okta", r"okta"), ("Auth0", r"auth0"), ("OneLogin", r"onelogin")]
# sites that are more than a sign-in service: only their sign-in routes count
_IDP_PATHS = re.compile(r"^(?:www\.)?(?:facebook\.com|github\.com|linkedin\.com|twitter\.com|x\.com)$", re.I)
_SIGN_IN_PATH = re.compile(r"/(?:login|signin|sign-in|session|oauth|authorize|uas/|checkpoint)", re.I)
_SIGN_IN_TITLE = re.compile(r"^\s*(?:sign\s*in|log\s*in|sign\s+in\s+to\s+continue)\b.{0,60}?(?:google|microsoft|apple|"
                            r"facebook|github|linkedin|yahoo)\s+accounts?\b|^\s*sign\s+in\s*[-\u2013|]\s*google\s+accounts", re.I)


def identity_provider(url: str, snap: Snapshot | None = None) -> str:
    """'Google' / 'Microsoft' ... when the page is that service's own sign-in page, else ''."""
    parsed = urlparse(url or "")
    host = parsed.netloc.lower().split(":")[0]
    known = bool(_IDP_HOSTS.search(host)) or (bool(_IDP_PATHS.match(host)) and bool(_SIGN_IN_PATH.search(parsed.path)))
    titled = snap is not None and bool(_SIGN_IN_TITLE.search(snap.title or ""))
    if not (known or titled):
        return ""
    text = host if known else (snap.title or "")
    return next((name for name, rx in _IDP_NAMES if re.search(rx, text, re.I)),
                host.split(".")[-2].title() if known and "." in host else "another service")


# what marks a form as a job application (see ApplicationNavigator._is_job_application)
_APPLICATION_FIELD = re.compile(r"resume|\bcv\b|curriculum|cover\s+(?:letter|note|message)|notice|experience|\bctc\b|salary|compensation|"
                                r"position|appl(?:y|ying|ied)\s+for|job\s+(?:role|profile|title|code|id)|designation|"
                                r"qualification|education|\bskills?\b|linked\s*in|portfolio|git\s*hub|current\s+(?:company|"
                                r"employer)|date\s+of\s+birth|relocat|work\s+authori[sz]ation|sponsorship", re.I)
_SALES_SECTION = re.compile(r"\bprojects?\b|\bquote\b|contact\s+us|get\s+in\s+touch|let'?s\s+(?:talk|build|work|connect)|"
                            r"talk\s+to\s+(?:us|sales|an?\s+expert)|enquir|inquir|newsletter|subscribe|\bdemo\b|consultation|"
                            r"call\s*back|\bsales\b|partner\s+with|feedback|how\s+can\s+we\s+help|hire\s+(?:us|our|a\b)", re.I)
_APPLY_URL = re.compile(r"(?:^|[/_.-])(?:apply|applying|application|applications|applicant|job-?form|careers?-?form|"
                        r"candidate)(?:$|[/_.?-])", re.I)
_APPLY_ACTION = re.compile(r"\bappl(?:y|ication)\b|(?:submit|send)\s+(?:my\s+|your\s+)?(?:application|candidature|resume|cv|"
                           r"profile)|join\s+(?:us|our\s+team)", re.I)


class ApplicationNavigator:
    def __init__(self, engine: BrowserEngine, ctx: ApplyContext, *, reasoner: PageReasoner | None = None,
                 cta_submits: bool = False, max_steps: int | None = None, max_minutes: float | None = None,
                 ats_name: str | None = None, dialog_only: bool = False) -> None:
        s = get_settings()
        self.engine = engine
        self.ctx = ctx
        self.reasoner = reasoner
        self.cta_submits = cta_submits  # portals where the job page "Apply" click is itself the submission
        self.max_steps = max_steps or s.navigator_max_steps
        self.max_seconds = (max_minutes or s.navigator_max_minutes) * 60
        self.ats_name = ats_name
        # A hand-off inside a portal's own apply dialog (LinkedIn Easy Apply): only that dialog is the application.
        # The page behind it belongs to the portal - its "Save", "Follow" and "I'm interested" buttons act on your
        # account there, and are never touched.
        self.dialog_only = dialog_only
        self.delegated: set[str] = {ats_name} if ats_name else set()
        self.filler: FormFiller = ctx.form_filler()
        self.history: list[str] = []
        self.visits: Counter[str] = Counter()
        self.tried: dict[str, set[str]] = defaultdict(set)  # page signature -> element ids clicked there
        self.scrolled: set[str] = set()
        self.dead: Counter[tuple[str, str]] = Counter()  # (url, label) clicks that changed nothing
        self.planner_calls = 0
        self.submitted = False
        self.reclicked = False  # one more click only when the first did nothing at all
        self._watcher: SubmissionWatcher | None = None
        self.pre_submit_success = ""
        self.pre_submit_signature = ""
        self.filled: set[str] = set()  # field ids already handled (pending answers are asked once)
        self.unresolved: list[str] = []
        self.asked: set[str] = set()
        self.application_evidence: list[str] = []  # what shows the form being filled is a job application
        self.resume_sent = False  # a résumé / document was attached to the form in this application
        self.refused = False  # a submit click was refused by the form's validation (and none went through since)
        self.waited_for: set[str] = set()  # blockers you were already asked to clear on this application
        self.ai_choices: Counter[str] = Counter()  # what the local AI advised to click (to notice a loop)
        self.cookie_done = False
        self.closed_dialogs: set[str] = set()
        self.signin_attempts = 0
        self.signup_attempts = 0
        self.site_creds: dict[str, SiteCredential | None] = {}  # looked up once per site
        self.google_started = False  # the site's "Sign in with Google" was opened in this application
        self.idp_waits = 0  # times you were asked to finish a sign-in on an identity provider's page
        self.parents: dict[int, Page] = {}  # tab a pop-up was opened from: where the application goes on
        self.steps = 0
        self.human_assisted = False
        self.first_success: str | None = None
        self.start_url = ""
        self.page: Page | None = None
        self.own_pages: list[Page] = []

    # ================================================================== run
    async def run(self, page: Page) -> ApplyResult:
        self.page = page
        self.start_url = page.url
        self.engine.use_page(page)
        started = time.monotonic()
        await self.ctx.event("navigator", f"Navigating the application on {site_label(page.url)}", url=page.url)
        try:
            for n in range(self.max_steps):
                self.steps = n + 1
                await self.engine.checkpoint()
                if time.monotonic() - started > self.max_seconds:
                    return await self._manual(f"Application on {site_label(self.page.url)} took too long", stuck=True)
                page = self._active_page()
                await self._settle(page)
                snap = await observe(page)
                self.visits[snap.signature] += 1
                await self._step(page, snap)
            return await self._manual(f"Too many steps on {site_label(self.page.url)}", stuck=True)
        except Finish as done:
            return done.result
        finally:
            await self._close_own_pages()

    # ================================================================ steps
    async def _step(self, page: Page, snap: Snapshot) -> None:
        if self.dialog_only:
            if not snap.dialog_open and not self.submitted:
                # (run #53, LinkedIn: the dialog was gone at hand-off. On the job page behind it the navigator clicked
                # "Easy Apply" twice, the local model then chose "I'm interested", and "Save" saved the job.)
                self.refused = True  # nothing was sent: the job may simply be tried again
                await self._stop_manual(f"The apply dialog on {site_label(page.url)} closed before the application was "
                                        "finished - nothing was sent. Retry opens it again")
            snap.elements = [e for e in snap.elements if e.dialog]
            snap.fields = [f for f in snap.fields if f.dialog]
        # ---- 1. outcomes visible on the page
        if self.first_success is None:
            self.first_success = snap.success
        if self.submitted and snap.success and (snap.signature != self.pre_submit_signature
                                                or snap.success != self.pre_submit_success):
            raise Finish(self._applied(snap.success, page))
        if self.human_assisted and snap.success and snap.success != self.first_success:
            self.submitted = True  # you finished it in the browser while the agent was waiting
            raise Finish(self._applied(snap.success, page))
        if snap.already_applied and not self.submitted and len(snap.app_fields) < 2:
            raise Finish(ApplyResult(ApplyOutcome.ALREADY_APPLIED, f"{site_label(snap.url)} says you already applied",
                                     ats=self._ats(page), external_url=page.url))
        if snap.expired and not snap.app_fields and not snap.by_kind(Kind.APPLY):
            raise Finish(ApplyResult(ApplyOutcome.EXPIRED, f"Job is closed: \"{snap.expired}\"", ats=self._ats(page),
                                     external_url=page.url))

        # ---- 2. challenges that need a human (never bypassed)
        blockers = await self.engine.detect_blockers(page, include_widgets=False)
        if blockers:
            await self._human(page, blockers[0], {"captcha": "Please solve the CAPTCHA / security check",
                                                  "otp": "Please enter the verification code (OTP)"}[blockers[0]]
                              + f" on {site_label(page.url)}, then press Continue")
            return

        # ---- 2b. somebody else's sign-in page (Google, Microsoft, Apple ...): yours alone - nothing is typed there
        provider = identity_provider(page.url, snap)
        if provider:
            self.idp_waits += 1
            if self.idp_waits > 2:  # asked twice, still on the sign-in page: yours to finish, not a loop to stay in
                await self._stop_manual(f"The {provider} sign-in for {site_label(self.start_url)} was not completed. "
                                        "Sign in there yourself, then use Retry - the application continues signed in")
            await self.ctx.event("sign_in_page", f"The application leads to a {provider} sign-in page - nothing was "
                                                 "entered there", level="warning", url=page.url)

            async def signed_in() -> bool:  # the sign-in window closed, or the tab left the provider's pages
                return page.is_closed() or not identity_provider(page.url)

            await self._human(page, "account", f"This application needs you to sign in with {provider}. The assistant "
                                               "never types into another service's sign-in page: sign in yourself in "
                                               "the browser (account, password, any verification or consent) - the "
                                               "application continues by itself when you are done", done=signed_in)
            return
        if self.google_started and self.idp_waits and "google_done" not in self.waited_for:
            self.waited_for.add("google_done")
            await self.ctx.event("signed_in", f"Back on {site_label(page.url)} after the Google sign-in: continuing "
                                              "the application", url=page.url)

        # ---- 3. overlays: cookie banners and popups that hide the page
        if await self._handle_overlays(page, snap):
            return

        # ---- 3b. a known application system: its specialised adapter goes first
        if await self._delegate_known_ats(page):
            return

        # ---- 4. accounts (a login widget on a job page that still offers "Apply" is not a wall yet)
        untried_apply = [e for e in snap.by_kind(Kind.APPLY) if e.id not in self.tried[snap.signature]]
        if self._account_wall(snap) and (not untried_apply or snap.dialog_open):
            await self._handle_account(page, snap)
            return
        if VERIFY_EMAIL_TEXT.search(snap.text[:3000]) and not snap.app_fields:
            await self._human(page, "verify", f"{site_label(page.url)} sent a verification email to "
                                              f"{self.ctx.profile.personal.email or 'your email'}. Open the link (or "
                                              f"enter the code) in the agent's browser, then press Continue")
            return

        # ---- 5. an application form
        if self._is_form(snap):
            await self._handle_form(page, snap)
            return

        # ---- 6. a job page with an Apply button, or an intermediate choice page
        for kind in (Kind.APPLY, Kind.GUEST, Kind.NEXT):
            el = self._best_button(snap, kind)
            if el is not None:
                if kind == Kind.APPLY and self.cta_submits:
                    await self._submit(page, snap, el, reason="Apply on this portal sends the application")
                else:
                    await self._click_and_record(page, snap, el, f"Clicked '{el.label}'")
                return
        if snap.by_kind(Kind.SIGNIN, Kind.SIGNUP) and not snap.by_kind(Kind.APPLY):
            await self._handle_account(page, snap)
            return

        # ---- 7. nothing obvious: reveal lazy content, then ask the local model, then you
        await self._fallback(page, snap)

    # ============================================================ outcomes
    def _ats(self, page: Page | None = None) -> str:
        if self.ats_name:
            return self.ats_name
        return ("site:" + site_label((page or self.page).url if (page or self.page) else self.start_url))[:64]

    def _applied(self, text: str, page: Page, *, evidence: list[str] | None = None) -> ApplyResult:
        return ApplyResult(ApplyOutcome.APPLIED, f"Applied on {site_label(page.url)}", confirmation_text=text,
                           ats=self._ats(page), submit_attempted=True, external_url=page.url,
                           unresolved=self.unresolved, verification="confirmed",
                           evidence=list(evidence or [f"confirmation: {text[:160]}"]))

    async def _manual(self, reason: str, *, stuck: bool = False) -> ApplyResult:
        page = self.page
        shot = None
        url = None
        if page is not None and not page.is_closed():
            shot = await self.engine.screenshot("navigator-manual", page=page)
            url = page.url
        await self.ctx.event("needs_you", reason, level="warning", url=url or "")
        return ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED, reason, ats=self._ats(page) if page else self.ats_name,
                           submit_attempted=self.submitted, screenshot=shot, external_url=url,
                           unresolved=list(self.unresolved),
                           # the only submit click was refused by the form's own validation: nothing was sent
                           nothing_sent=self.refused and not self.submitted)

    async def _stop_manual(self, reason: str) -> None:
        raise Finish(await self._manual(reason))

    async def _human(self, page: Page, kind: str, reason: str, *,
                     done: Callable[[], Awaitable[bool]] | None = None) -> None:
        """Pause for you; continue from the same page afterwards (or park the job when nobody responds).
        ``done``: how the assistant can see for itself that you finished (it then continues without "Continue")."""
        if self.ctx.mode.value == "dry_run":
            await self.ctx.step(f"Dry run: would need you here - {reason}", page.url, kind=kind)
            raise Finish(ApplyResult(ApplyOutcome.DRY_RUN, f"Dry run stopped: {reason}", ats=self._ats(page),
                                     external_url=page.url, unresolved=list(self.unresolved)))
        await self.ctx.step(f"Waiting for you: {reason}", page.url, kind=kind)
        before = await self.engine.page_signature(page)
        widget = kind == "captcha" and await self.engine.captcha_widget_present(page)

        async def cleared() -> bool:
            if kind not in {"captcha", "otp"}:
                return False
            if await self.engine.detect_blockers(page, include_widgets=False):
                return False
            # an inline "I'm not a robot" box (or a sum to type) is done when it carries its proof - not merely
            # because no challenge page is in the way (the form would be sent unsolved and refused)
            return await self.engine.captcha_widget_solved(page) if widget else True

        res = await self.ctx.wait_for_human(kind, reason, page=page,
                                            detect_done=cleared if kind in {"captcha", "otp"} else done)
        if res in (GateResult.RESOLVED, GateResult.APPROVE, GateResult.SUBMITTED):
            self.history.append(f"you completed: {kind}")
            self.human_assisted = True
            self.visits.clear()  # the page may legitimately look the same afterwards
            if not page.is_closed() and await self.engine.page_signature(page) == before \
                    and kind not in {"captcha", "otp"}:
                await asyncio.sleep(0.5)
            return
        if res == GateResult.REJECT:
            raise Finish(ApplyResult(ApplyOutcome.REJECTED, "You chose to skip this application", ats=self._ats(page)))
        raise Finish(await self._manual(reason))

    async def _delegate_known_ats(self, page: Page) -> bool:
        from app.ats.registry import detect_ats

        cls = await detect_ats(page)
        if cls is None or cls.name in self.delegated:
            return False
        self.delegated.add(cls.name)
        self.ats_name = cls.name
        await self.ctx.step(f"Recognised {cls.display_name}: using its specialised steps", page.url)
        result = await cls(self.engine).apply(page, self.ctx)
        if not result.handoff:
            raise Finish(result)
        await self.ctx.event("handoff", f"{cls.display_name} adapter stuck ({result.message}); continuing generically",
                             level="warning")
        self.page = self.engine.page if not self.engine.page.is_closed() else page
        return True

    # ============================================================ overlays
    async def _handle_overlays(self, page: Page, snap: Snapshot) -> bool:
        if snap.cookie_banner and not self.cookie_done:
            cookies = snap.by_kind(Kind.COOKIES)
            if cookies:
                prefer = re.compile(r"reject|decline|necessary|essential", re.I)
                el = next((e for e in cookies if prefer.search(e.label)), None) or next(
                    (e for e in cookies if re.search(r"accept|allow|agree|got it|ok", e.label, re.I)), cookies[0])
                self.cookie_done = True
                if await self._click(page, snap, el):
                    self.history.append(f"cookie banner: {el.label}")
                    await asyncio.sleep(0.4)
                    return True
        # a popup that is not the application (newsletter, talent community, chat, survey...)
        if snap.dialog_open and any(PROMO_DIALOG_RX.search(d[:400]) for d in snap.dialogs):
            closers = [e for e in snap.elements if e.dialog and e.kind == Kind.CLOSE and not e.disabled]
            if closers and snap.signature not in self.closed_dialogs:
                self.closed_dialogs.add(snap.signature)
                if await self._click(page, snap, closers[0]):
                    self.history.append(f"closed promotional popup: {closers[0].label}")
                    await asyncio.sleep(0.4)
                    return True
        if snap.dialog_open:
            dialog_fields = [f for f in snap.app_fields if f.dialog]
            useful = [e for e in snap.elements if e.dialog and e.kind in {
                Kind.APPLY, Kind.NEXT, Kind.SUBMIT, Kind.GUEST, Kind.SIGNIN, Kind.SIGNUP, Kind.UPLOAD}]
            if not dialog_fields and not useful and not snap.password_count:
                closers = [e for e in snap.elements if e.dialog and e.kind == Kind.CLOSE and not e.disabled]
                key = snap.signature
                if closers and key not in self.closed_dialogs:
                    self.closed_dialogs.add(key)
                    el = closers[0]
                    if await self._click(page, snap, el):
                        self.history.append(f"closed popup: {el.label}")
                        await asyncio.sleep(0.4)
                        return True
                if key not in self.closed_dialogs:
                    self.closed_dialogs.add(key)
                    try:
                        await page.keyboard.press("Escape")
                        await asyncio.sleep(0.4)
                        return True
                    except PlaywrightError:
                        pass
        return False

    # ============================================================ accounts
    def _has_real_application_fields(self, snap: Snapshot) -> bool:
        """Résumé upload or several real questions next to the password box: an application form, not a wall."""
        if any(f.field.type == "file" for f in snap.app_fields):
            return True
        app = [f for f in snap.app_fields if f.field.type not in {"password", "checkbox"}
               and not _EMAIL_FIELD.search(f.field.label + " " + f.field.name)
               and not re.search(r"\b(?:first|last|full|given|family|sur)\s*name\b|^name$", f.field.label, re.I)]
        return len(app) >= 3

    def _account_wall(self, snap: Snapshot) -> bool:
        if not snap.password_count:
            return False
        if any(f.field.type == "file" for f in snap.app_fields):
            return False  # an application form that also asks for a password
        return self._looks_like_signup(snap) or not self._has_real_application_fields(snap)

    def _credential(self, address: str) -> SiteCredential | None:
        """The sign-in the assistant may use on ``address``: the one you stored for that site, else the general
        job-site password with your profile e-mail (looked up once per site and application)."""
        key = normalize_site(address)
        if key not in self.site_creds:
            self.site_creds[key] = lookup_credential(address, default_username=self.ctx.profile.personal.email) if key else None
        return self.site_creds[key]

    def _frame_credential(self, page: Page, frame: Frame) -> SiteCredential | None:
        """What may be typed into a password box of ``frame``: nothing on an identity provider's page, and nothing
        in a box framed in from another site unless a sign-in is stored for that very site."""
        url = frame.url if frame.url.startswith("http") else page.url
        if identity_provider(url):
            return None
        cred = self._credential(url)
        if cred is None or cred.site_specific or same_site(url, page.url):
            return cred
        return None

    def _google_button(self, snap: Snapshot) -> Element | None:
        """The site's own "Sign in with Google" / "Continue with Google" control, if it offers one."""
        found = [e for e in snap.by_kind(Kind.SSO) if re.search(r"\bgoogle\b", e.label, re.I)]
        return found[0] if found else None

    async def _sign_in_with_google(self, page: Page, snap: Snapshot, el: Element) -> None:
        """Open the site's Google sign-in. Google's page is yours: the next step waits there for you (choosing the
        account, password, 2-step verification, consent) and types nothing. With a Google session this browser
        profile already has - and a site you approved before - Google sends the tab straight back, signed in."""
        self.waited_for.add("google")
        self.google_started = True
        await self.ctx.event("google_sign_in", f"{site_label(page.url)} offers '{el.label}': opening Google's own "
                                               "sign-in page (nothing is typed there by the assistant)", url=page.url)
        await self._click_and_record(page, snap, el, f"Opened '{el.label}' on {site_label(page.url)}")

    async def _handle_account(self, page: Page, snap: Snapshot) -> None:
        site = site_label(page.url)
        guest = self._best_button(snap, Kind.GUEST)
        if guest is not None:
            await self._click_and_record(page, snap, guest, f"Continued without an account ('{guest.label}')")
            return
        settings = get_settings()
        cred = self._credential(page.url)
        google = self._google_button(snap) if settings.google_signin and "google" not in self.waited_for else None
        if google is not None and self.ctx.mode.value != "dry_run" and (cred is None or self.signin_attempts >= 1):
            # no password for this site (or it was not accepted) and the site offers Google: that is your sign-in
            await self._sign_in_with_google(page, snap, google)
            return
        how = (f"store a sign-in for it with `python -m app.cli site-login set {normalize_site(page.url) or site}`, "
               "or set PJA_ATS_ACCOUNT_PASSWORD")
        signup = self._looks_like_signup(snap)
        if not snap.password_count:
            # a choice page: "Sign in" / "Create account" buttons without a form yet
            target = None
            if cred is not None:
                target = (self._best_button(snap, Kind.SIGNUP)
                          if self.signin_attempts and self._may_create_account(cred)
                          else self._best_button(snap, Kind.SIGNIN))
            if target is not None:
                await self._click_and_record(page, snap, target, f"Opened '{target.label}'")
                return
            await self._human(page, "account", f"{site} requires you to sign in or create an account. Please do it "
                                               "in the agent's browser window, then press Continue")
            return
        if cred is None:
            await self._human(page, "account", f"{site} asks you to sign in or create an account. Please do it in the "
                                               f"agent's browser window, then press Continue (to let the assistant "
                                               f"sign in here next time: {how})")
            return
        if signup:
            if cred.site_specific:
                # the sign-in you stored is for an account that exists: it is never used to register a new one
                signin = self._best_button(snap, Kind.SIGNIN)
                if signin is not None and signin.id not in self.tried[snap.signature]:
                    await self._click_and_record(page, snap, signin, f"Opened '{signin.label}' (a sign-in is stored for {site})")
                    return
                await self._human(page, "account", f"{site} shows a registration form, and the sign-in stored for it is "
                                                   "for an existing account. Please sign in (or register) in the "
                                                   "agent's browser, then press Continue")
                return
            if self.ctx.mode.value == "dry_run":
                raise Finish(ApplyResult(ApplyOutcome.DRY_RUN, f"Dry run: {site} needs a new account (not created in "
                                                               "Dry Run)", ats=self._ats(page), external_url=page.url))
            if not self._may_create_account(cred) or self.signup_attempts >= 2:
                await self._human(page, "account", f"Please create the account on {site}, then press Continue")
                return
            if ACCOUNT_EXISTS_TEXT.search(snap.text) and self.signup_attempts:
                signin = self._best_button(snap, Kind.SIGNIN)
                if signin is not None:
                    await self._click_and_record(page, snap, signin, "Account exists: switching to sign in")
                    return
            self.signup_attempts += 1
            await self._fill_credentials(page, snap, signup=True)
            btn = self._account_button(snap, signup=True)
            if btn is None:
                await self._human(page, "account", f"Could not find the create-account button on {site}; please "
                                                   "finish creating the account, then press Continue")
                return
            await self.ctx.event("account", f"Creating an account on {site} with {self.ctx.profile.personal.email}")
            await self._click_and_record(page, snap, btn, f"Create account on {site}")
            return
        # sign in
        if self.signin_attempts >= 1:
            if LOGIN_ERROR_TEXT.search(snap.text) and self._may_create_account(cred) and self.signup_attempts == 0:
                link = self._best_button(snap, Kind.SIGNUP)
                if link is not None and self.ctx.mode.value != "dry_run":
                    await self._click_and_record(page, snap, link, f"No account yet: opening '{link.label}'")
                    return
            if self.signin_attempts >= 2 or LOGIN_ERROR_TEXT.search(snap.text):
                stored = f"The sign-in stored for {cred.site} was not accepted by {site}" if cred.site_specific else \
                    f"Signing in to {site} did not work"
                await self._human(page, "account", f"{stored}. Please sign in (or create the account) in the agent's "
                                                   "browser, then press Continue")
                return
        self.signin_attempts += 1
        if not await self._fill_credentials(page, snap, signup=False):
            await self._human(page, "account", f"Please sign in to {site} in the agent's browser, then press Continue "
                                               "(the sign-in box belongs to another service: nothing was typed)")
            return
        await self.ctx.event("account", f"Signing in to {site} with the "
                             + (f"sign-in stored for {cred.site}" if cred.site_specific else "general job-site password"))
        btn = self._account_button(snap, signup=False)
        if btn is None:
            try:
                frame = self._frame_of_password(snap)
                await frame.locator("input[type='password']").first.press("Enter")
            except PlaywrightError:
                await self._human(page, "account", f"Please sign in to {site}, then press Continue")
                return
        else:
            await self._click_and_record(page, snap, btn, f"Signing in to {site}")

    def _may_create_account(self, cred: SiteCredential | None = None) -> bool:
        """Accounts are only ever created with the general job-site password (never with a sign-in stored for an
        existing account), and only when you allow it."""
        s = get_settings()
        if cred is not None and cred.site_specific:
            return False
        return bool(s.ats_account_password and s.ats_create_accounts)

    def _looks_like_signup(self, snap: Snapshot) -> bool:
        if snap.password_count >= 2 or any(CONFIRM_PASSWORD_RX.search(p) for p in snap.password_fields):
            return True
        heads = " ".join(snap.headings[:5])
        return bool(_SIGNUP_TEXT.search(heads)) and not re.search(r"sign\s*in|log\s*in", heads, re.I)

    def _frame_of_password(self, snap: Snapshot) -> Frame:
        idx = next((f.frame for f in snap.fields if f.field.type == "password"), 0)
        return snap.frame(idx) if idx < len(snap.frames) else self.page.main_frame  # type: ignore[union-attr]

    async def _fill_passwords(self, frame: Frame) -> int:
        """A password to choose inside an application form (an account made along the way): the general job-site
        password only."""
        password = get_settings().ats_account_password
        if not password or identity_provider(frame.url):
            return 0
        try:
            boxes = frame.locator("input[type='password']:visible")
            n = await boxes.count()
            for i in range(n):
                if not (await boxes.nth(i).input_value()):
                    await boxes.nth(i).fill(password)
            return n
        except PlaywrightError:
            return 0

    async def _fill_credentials(self, page: Page, snap: Snapshot, *, signup: bool) -> bool:
        """Type the user name and password into the sign-in form. False when the only password box belongs to
        another service (nothing is typed then)."""
        for info in snap.frames:
            frame = info.frame
            try:
                pw = frame.locator("input[type='password']:visible")
                n = await pw.count()
            except PlaywrightError:
                continue
            if not n:
                continue
            cred = self._frame_credential(page, frame)
            if cred is None or (signup and cred.site_specific):
                continue
            for i in range(n):
                await pw.nth(i).fill(cred.password)
            # username / email box
            for f in snap.fields:
                if f.frame != info.index or f.field.type in {"password", "file", "checkbox", "radio"}:
                    continue
                blob = f"{f.field.label} {f.field.name} {f.field.placeholder}"
                if f.field.type == "email" or _EMAIL_FIELD.search(blob) or (
                        not signup and re.search(r"user\s*(?:name|id)|login\s*(?:name|id)", blob, re.I)):
                    try:
                        await frame.locator(f.field.selector).first.fill(cred.username)
                    except PlaywrightError:
                        pass
            if signup:  # names, phone, terms checkboxes: the regular filler knows how to answer those
                skip = {f.field.id for f in snap.fields if f.field.type == "password"}
                report = await self.filler.fill(frame, skip=skip)
                self._note_unresolved(report)
            return True
        return False

    def _account_button(self, snap: Snapshot, *, signup: bool) -> Element | None:
        want = (Kind.SIGNUP, Kind.SUBMIT, Kind.NEXT) if signup else (Kind.SIGNIN, Kind.NEXT, Kind.SUBMIT)
        pw_forms = {f.form for f in snap.fields if f.field.type == "password" and f.form}
        cands = [e for e in snap.elements if e.kind in want and not e.disabled and e.kind != Kind.SSO]
        if pw_forms:
            in_form = [e for e in cands if e.form in pw_forms]
            cands = in_form or cands
        if not cands:
            return None
        return sorted(cands, key=lambda e: (want.index(e.kind), 0 if e.form in pw_forms else 1, -e.y))[0]

    # =============================================================== forms
    def _is_form(self, snap: Snapshot) -> bool:
        fields = [f for f in snap.app_fields if f.field.type not in {"password"}]
        if not fields:
            return False
        if any(f.field.type == "file" for f in fields) or len(fields) >= 2:
            return True
        # a single field (e.g. "Enter your email to start") counts when it has its own Next/Submit button
        return bool(snap.by_kind(Kind.NEXT, Kind.SUBMIT))

    def _scope(self, snap: Snapshot) -> tuple[int, set[str], bool]:
        """Which frame / fields belong to the application: a dialog with fields wins over the page behind it."""
        fields = [f for f in snap.app_fields if f.field.type != "password"]
        in_dialog = [f for f in fields if f.dialog]
        chosen = in_dialog or fields
        frame_idx = Counter(f.frame for f in chosen).most_common(1)[0][0]
        keep = {f.field.id for f in chosen if f.frame == frame_idx}
        skip = {f.field.id for f in snap.fields if f.frame == frame_idx and f.field.id not in keep}
        return frame_idx, skip, bool(in_dialog)

    async def _handle_form(self, page: Page, snap: Snapshot) -> None:
        frame_idx, skip, in_dialog = self._scope(snap)
        frame = snap.frame(frame_idx)
        if any(f.frame == frame_idx and f.field.type == "password" for f in snap.fields):
            if not await self._fill_passwords(frame):
                await self._human(page, "account", f"The application form on {site_label(page.url)} asks you to choose "
                                                   "a password. Please fill it in the browser (or set "
                                                   "PJA_ATS_ACCOUNT_PASSWORD), then press Continue")
        n_fields = len([f for f in snap.app_fields if f.frame == frame_idx and f.field.id not in skip])
        await self.ctx.step(f"Filling an application form on {site_label(page.url)} ({n_fields} field(s))", page.url)
        self.filler.page_context = "\n".join(snap.headings[:6]) + "\n" + snap.text[:1200]
        report = await self.filler.fill(frame, skip=skip)
        # conditional questions appear after answering earlier ones: one more pass over new fields
        snap2 = await observe(page)
        skip |= {f.field.id for f in snap2.fields if f.frame == frame_idx and not f.application}
        new_ids = {f.field.id for f in snap2.app_fields if f.frame == frame_idx} - {i.field.id for i in report.items} - skip
        if new_ids:
            more = await self.filler.fill(frame, skip=skip | {i.field.id for i in report.items})
            report.items.extend(more.items)
            report.resume_uploaded = report.resume_uploaded or more.resume_uploaded
            snap = snap2
        else:
            snap = snap2
        await self._maybe_upload_resume(page, snap, report)
        self._note_application_evidence(snap, frame_idx, report)
        filled = [i for i in report.items if i.status in {"filled", "uploaded"}]
        self.filled.update(i.field.id for i in filled)
        await self._resolve_questions(page, frame, report)

        button = self._progress_button(snap, frame_idx, in_dialog)
        if button is None:
            locked = self._locked_submit(snap, frame_idx, in_dialog)
            if locked is not None and await self.engine.captcha_widget_present(page) \
                    and not await self.engine.captcha_widget_solved(page) and "captcha" not in self.waited_for:
                self.waited_for.add("captcha")  # asked once: an unanswered request parks the job with this reason
                await self._human(page, "captcha", f"{site_label(page.url)} has a CAPTCHA on the form and keeps "
                                                   f"'{locked.label}' disabled until it is solved. Please solve it in the "
                                                   "browser (do not submit), then press Continue")
                return
            await self._fallback(page, snap, reason=(
                f"filled the form, but its '{locked.label}' button stays disabled - the page is waiting for something "
                "the assistant could not provide" if locked is not None else "could not find a Next or Submit button"))
            return
        if button.kind in {Kind.SUBMIT} or (button.kind == Kind.OTHER and button.type == "submit"):
            await self._submit(page, snap, button)
            return
        await self._next(page, snap, button, frame)

    async def _maybe_upload_resume(self, page: Page, snap: Snapshot, report: FillReport) -> None:
        """Sites with a custom 'Upload resume' button and no visible file input: use the file chooser."""
        has_file = any(i.field.type == "file" for i in report.items)
        resume = self.ctx.resume_path
        if has_file or report.resume_uploaded or resume is None or not Path(resume).is_file():
            return
        if "resume" in getattr(self.filler, "_handled", set()):
            return
        uploads = [e for e in snap.by_kind(Kind.UPLOAD) if re.search(r"resume|cv|upload|attach|browse", e.label, re.I)]
        if not uploads:
            return
        el = uploads[0]
        frame = snap.frame(el.frame)
        try:
            async with page.expect_file_chooser(timeout=5000) as fc:
                await frame.locator(el.selector).first.click(timeout=5000)
            chooser = await fc.value
            await chooser.set_files(str(resume))
            self.filler.mark_resume_handled()
            report.resume_uploaded = True
            await self.ctx.event("resume", f"Uploaded {Path(resume).name} via '{el.label}'")
            await asyncio.sleep(1.0)
        except (PlaywrightError, PlaywrightTimeout) as exc:
            log.debug("file chooser upload failed: %s", exc)

    def _note_application_evidence(self, snap: Snapshot, frame_idx: int, report: FillReport | None = None) -> None:
        """Remember what makes this form a job application (kept across the steps of a multi-page form)."""
        found: list[str] = []
        if report is not None and report.resume_uploaded:
            self.resume_sent = True
            found.append("your résumé is attached")
        fields = [f for f in snap.app_fields if f.frame == frame_idx]
        if any(f.field.type == "file" for f in fields):
            found.append("the form takes a document")
        named = next((f.field.label for f in fields if _APPLICATION_FIELD.search(f.field.label or "")), "")
        if named:
            found.append(f"it asks an application question ('{named[:40]}')")
        for item in found:
            if item not in self.application_evidence:
                self.application_evidence.append(item)

    def _is_job_application(self, snap: Snapshot, el: Element) -> bool:
        """The form about to be submitted is a job application - not the site's contact / sales / enquiry form,
        which often sits on a careers page with the same name, e-mail and phone boxes."""
        self._note_application_evidence(snap, el.frame)
        if _APPLY_ACTION.search(el.label or ""):
            return True  # "Submit application", "Apply now"
        if any(h.startswith("Clicked '") and _APPLY_ACTION.search(h) for h in self.history):
            return True  # the form was opened by an Apply button
        if self.application_evidence:
            return True  # a résumé, a document upload, a question about the job
        # A bare name / e-mail / message form. "Careers" in the page title is not enough: agencies put their sales
        # form on every page (rankraze.com asked for "Company Name"; nauticalinfosolutions.com answered "Thank you
        # for your inquiry"). Only the address of an application page counts.
        return bool(_APPLY_URL.search(urlparse(snap.url).path)) and not _SALES_SECTION.search(el.section or "")

    def _note_unresolved(self, report: FillReport) -> None:
        for i in report.unresolved:
            label = i.question or i.field.label or i.field.name
            if label and label not in self.unresolved:
                self.unresolved.append(label)

    async def _resolve_questions(self, page: Page, frame: Frame, report: FillReport) -> None:
        """Required questions the assistant could not answer confidently: ask you once, then fill + remember."""
        pending = [i for i in report.unresolved if i.field.id not in self.asked]
        if not pending:
            self._note_unresolved(report)
            return
        if self.ctx.mode.value == "dry_run":
            self._note_unresolved(report)
            await self.ctx.event("dry_run", f"{len(pending)} question(s) would need your answer",
                                 unresolved=[i.question for i in pending])
            return
        self.asked.update(i.field.id for i in pending)
        out = await ask_and_fill(self.ctx, page, frame, self.filler, pending)
        if out.skipped_by_you:
            raise Finish(ApplyResult(ApplyOutcome.REJECTED, "You chose to skip this application", ats=self._ats(page)))
        if not out.answered:
            self._note_unresolved(report)
            await self._stop_manual("Required question(s) need your answer: "
                                    + "; ".join(q[:80] for q in out.still_missing[:5]))
        for label in out.still_missing:
            if label not in self.unresolved:
                self.unresolved.append(label)

    def _locked_submit(self, snap: Snapshot, frame_idx: int, in_dialog: bool) -> Element | None:
        """The form's own Submit / Next button when the page keeps it disabled."""
        forms = {f.form for f in snap.app_fields if f.frame == frame_idx and f.form}
        return next((e for e in snap.elements if e.frame == frame_idx and e.disabled and (e.dialog or not in_dialog)
                     and (e.kind in {Kind.SUBMIT, Kind.NEXT} or e.type == "submit") and (not forms or e.form in forms)), None)

    def _progress_button(self, snap: Snapshot, frame_idx: int, in_dialog: bool) -> Element | None:
        scope_fields = [f for f in snap.app_fields if f.frame == frame_idx and (f.dialog or not in_dialog)]
        forms = Counter(f.form for f in scope_fields if f.form)
        main_form = forms.most_common(1)[0][0] if forms else ""
        last_y = max((f.y for f in scope_fields), default=0)
        tried = self.tried[snap.signature]
        cands: list[tuple[float, Element]] = []
        for e in snap.elements:
            if e.frame != frame_idx or e.disabled or e.id in tried:
                continue
            if in_dialog and not e.dialog:
                continue
            kind = e.kind
            if (kind == Kind.OTHER and e.type == "submit" and e.form and e.form == main_form
                    and not SEARCH_BUTTON_RX.search(norm(e.text or e.aria))):
                kind = Kind.SUBMIT
            if kind not in {Kind.NEXT, Kind.SUBMIT}:
                continue
            score = 0.0
            if main_form and e.form == main_form:
                score += 3
            if e.dialog and in_dialog:
                score += 3
            if e.y >= last_y - 10:
                score += 2
            if kind == Kind.NEXT:
                score += 1.5  # never jump past an intermediate step
            if classify_element(e.text, aria=e.aria, form_context=True) in {Kind.NEXT, Kind.SUBMIT}:
                score += 2  # its label says what it does (not just an untyped form button like "Add another")
            if e.tag in {"button", "input"}:
                score += 0.5
            if e.ctx:
                score -= 5
            cands.append((score, e))
        if not cands:
            return None
        cands.sort(key=lambda c: (-c[0], -c[1].y))  # ties: the bottom-most button moves the form forward
        return cands[0][1]

    async def _next(self, page: Page, snap: Snapshot, el: Element, frame: Frame) -> None:
        self.tried[snap.signature].add(el.id)
        changed = await self._click_and_record(page, snap, el, f"Next step ('{el.label}')")
        if changed:
            return
        errors = await collect_errors(frame)
        if not errors:
            return
        await self._handle_errors(page, frame, errors, after_submit=False)

    async def _handle_errors(self, page: Page, frame: Frame, errors: list[str], *, after_submit: bool) -> None:
        site = site_label(page.url)
        if after_submit:
            ok = next((e for e in errors if success_evidence(e)), "")
            if ok:  # some sites show their confirmation in the same box as validation messages
                raise Finish(self._applied(ok, page))
        errors = [e for e in errors if not success_evidence(e)]
        if not errors:
            return
        await self.ctx.event("form_errors", f"{site} reports: " + "; ".join(errors[:4]), level="warning")
        if await self.filler.repair(frame, errors):  # same fact, the format the site wants (+91..., 10 digits)
            await self.ctx.event("form_repair", f"Re-entered a value in the format {site} expects; trying again")
            self.tried.clear()
            return
        if self.ctx.mode.value == "dry_run":
            raise Finish(ApplyResult(ApplyOutcome.DRY_RUN, "Dry run stopped at form validation: " + "; ".join(errors[:3]),
                                     ats=self._ats(page), external_url=page.url, unresolved=self.unresolved))
        await self._human(page, "manual", f"{site} says: {'; '.join(errors[:3])}. Please fix these fields in the "
                                          "browser, then press Continue")
        self.tried.clear()

    # ============================================================== submit
    async def _submit(self, page: Page, snap: Snapshot, el: Element, reason: str = "") -> None:
        site = site_label(page.url)
        if (await self.engine.captcha_widget_present(page) and not self.submitted
                and not await self.engine.captcha_widget_solved(page)):  # (already solved: you are not asked again)
            if self.ctx.mode.value != "dry_run":
                await self._human(page, "captcha", f"{site} has a CAPTCHA on the form. Please tick/solve it in the "
                                                   "browser (do not submit), then press Continue")
        if not reason and not self.ats_name and not self.submitted and not self._is_job_application(snap, el):
            await self.ctx.event("not_an_application", f"The form on {site} ('{el.label}') takes no résumé and asks nothing "
                                                       "about a job: a contact / enquiry form - not submitted",
                                 level="warning")
            raise Finish(await self._manual(
                f"Not submitted: the only form on {site} is a contact / enquiry form ('{el.label}'), not a job "
                "application - it takes no résumé and asks nothing about the job. Apply to this employer yourself"))
        summary = (f"Application for '{self.ctx.job.title}' at {self.ctx.job.company} on {site} is filled"
                   + (f" ({reason})" if reason else "") + f" - '{el.label}' will submit it")
        before_success = snap.success

        async def user_submitted() -> str:
            if page.is_closed():
                return ""
            body = await self.engine.all_text(page, limit=8000)
            ev = success_evidence(body)
            return ev if ev and ev != before_success else ""

        await self._uncheck_optins(snap)
        decision = await self.ctx.decide_submit(page, unresolved=list(self.unresolved), summary=summary,
                                                detect_submitted=user_submitted)
        if decision == SubmitDecision.DRY_RUN:
            await self.ctx.step(f"Dry run: stopped before '{el.label}' on {site}", page.url)
            raise Finish(ApplyResult(ApplyOutcome.DRY_RUN, f"Dry run: application on {site} prepared, not submitted",
                                     ats=self._ats(page), external_url=page.url, unresolved=list(self.unresolved)))
        if decision == SubmitDecision.REJECTED:
            raise Finish(ApplyResult(ApplyOutcome.REJECTED, "You rejected this application", ats=self._ats(page)))
        if decision == SubmitDecision.MANUAL:
            why = ("Not submitted - needs your answers: " + "; ".join(self.unresolved[:5])) if self.unresolved else \
                "Not submitted - needs your review"
            raise Finish(await self._manual(why))
        if decision == SubmitDecision.SUBMITTED_BY_USER:
            text = await user_submitted()
            self.submitted = True
            if text:
                raise Finish(self._applied(text, page))
            raise Finish(await self._manual("You submitted it yourself but no confirmation was detected - please "
                                            "verify"))
        self.pre_submit_success = before_success
        self.pre_submit_signature = snap.signature
        frame = snap.frame(el.frame) if el.frame < len(snap.frames) else page.main_frame
        self._watcher = SubmissionWatcher(page, frame=frame, submit_selector=el.selector,
                                          portal_probe=self._listed_probe(page, snap),
                                          resume_attached=self.resume_sent or bool(reason) or bool(self.ats_name))
        await self._watcher.begin()
        await self.ctx.mark_submit_attempt()
        self.submitted = True
        self.refused = False
        self.tried[snap.signature].add(el.id)
        await self.ctx.step(f"Submitting ('{el.label}') on {site}", page.url)
        try:
            await self._click(page, snap, el)
            await self._await_submit_result(page, snap)
        finally:
            if self._watcher is not None:  # never leave network listeners on the tab (exceptions, Finish, stop)
                self._watcher.stop()
                self._watcher = None

    def _listed_probe(self, page: Page, snap: Snapshot):
        """Candidate portals that land on 'My applications' after submitting: the job listed as submitted there
        (only on such a page, and only if it was not already shown before the click)."""
        before = application_listed(snap.text, self.ctx.job.title)

        async def probe() -> str:
            current = self._active_page()
            if current.is_closed() or not _APPLICATIONS_PAGE.search(current.url):
                return ""
            listed = application_listed(await self.engine.all_text(current, limit=10_000), self.ctx.job.title)
            return listed if listed and listed != before else ""

        return probe

    async def _uncheck_optins(self, snap: Snapshot) -> None:
        """Pre-ticked marketing boxes (follow company, newsletter, job alerts) are opt-ins you never asked for."""
        for f in snap.fields:
            if f.field.type != "checkbox" or f.field.value != "true" or f.field.required:
                continue
            if not _OPTIN.search(f.field.label):
                continue
            try:
                await snap.frame(f.frame).locator(f.field.selector).first.uncheck(timeout=3000)
                self.history.append(f"unticked opt-in: {f.field.label[:60]}")
            except PlaywrightError:
                continue

    async def _await_submit_result(self, page: Page, before: Snapshot, wait_s: float = 25) -> None:
        """SUBMIT_CLICKED -> WAITING_FOR_SUBMISSION_RESULT -> CONFIRMED / UNCERTAIN / FAILED (engine/submission.py).
        Never submits again after an uncertain result; only a refused (validation) or ignored click is retried."""
        watcher = self._watcher
        assert watcher is not None
        rounds = 0
        while True:
            verdict = await watcher.wait(self._active_page, wait_s=wait_s)
            page = self._active_page()
            # the same rule as the 'solved?' check in _human: challenges, not an inline widget on the form
            blockers = await self.engine.detect_blockers(page, include_widgets=False) if not page.is_closed() else []
            if verdict.state == SubmissionState.BLOCKED and "captcha" not in blockers:
                blockers = ["captcha", *blockers]
            if blockers and verdict.state != SubmissionState.CONFIRMED:
                rounds += 1
                if rounds > 2:  # still blocked after you were asked twice: never loop - it is yours to finish
                    raise Finish(await self._result(ApplyOutcome.MANUAL_ACTION_REQUIRED,
                                                    f"A {blockers[0].upper()} blocks the submission on "
                                                    f"{site_label(page.url)} - only you can complete it", verdict))
                watcher.captcha_requests = 0  # after you solved it, watch the result again
                await self._human(page, blockers[0], f"Please complete the {blockers[0].upper()} on "
                                                     f"{site_label(page.url)} to finish submitting, then press Continue")
                continue
            break
        watcher.stop()
        self._watcher = None
        await self.ctx.event("submission", f"Submission result: {verdict.state.value}"
                             + (f" - {verdict.message}" if verdict.message else ""),
                             state=verdict.state.value, evidence=verdict.evidence)
        if verdict.state == SubmissionState.CONFIRMED:
            raise Finish(self._applied(verdict.confirmation_text, page, evidence=verdict.evidence))
        if verdict.state == SubmissionState.FAILED:
            raise Finish(await self._result(ApplyOutcome.FAILED, f"Submission failed on {site_label(page.url)}: "
                                            f"{verdict.message}", verdict))
        if verdict.state == SubmissionState.REFUSED:
            self.submitted = False  # the site refused it (nothing was sent): the corrected form may be submitted
            self.refused = True
            await self._handle_errors(page, page.main_frame, [verdict.message] + await collect_errors(page),
                                      after_submit=True)
            return
        if verdict.state == SubmissionState.NEXT_STEP:
            return  # the submit opened another step (extra questions, EEO page...): keep going
        if verdict.state == SubmissionState.NO_EFFECT and not self.reclicked:
            self.reclicked = True  # nothing happened at all (no request, no change): the click was lost
            await self.ctx.event("submit_retry", "The submit click had no effect - clicking it once more")
            self.submitted = False
            self.tried.clear()
            return
        if verdict.state == SubmissionState.NO_EFFECT:  # clicked twice; a script may still have sent it unseen
            raise Finish(await self._result(ApplyOutcome.MANUAL_ACTION_REQUIRED,
                                            f"Submitted - confirmation not verified on {site_label(page.url)}: the submit "
                                            "button showed no visible effect (clicked twice; no form request seen, the "
                                            "page did not change) - check in the browser whether it was sent", verdict))
        raise Finish(await self._result(ApplyOutcome.MANUAL_ACTION_REQUIRED,
                                        f"Submitted - confirmation not verified on {site_label(page.url)}: "
                                        f"{verdict.message or 'no confirmation the assistant could read'}", verdict))

    async def _result(self, outcome: ApplyOutcome, reason: str, verdict) -> ApplyResult:
        res = await self._manual(reason)
        res.outcome = outcome
        res.submit_attempted = True
        res.verification = verdict.verification or ("uncertain" if outcome == ApplyOutcome.MANUAL_ACTION_REQUIRED else "")
        res.evidence = verdict.evidence
        return res

    # ============================================================ fallback
    def _email_application(self, snap: Snapshot) -> str:
        """Career pages that only say "send your resume to hr@...": return that address."""
        apply_words = re.compile(r"apply|resume|\bcv\b|career|\bhr\b|recruit|hiring|talent|job", re.I)
        for e in snap.elements:
            if not e.href.lower().startswith("mailto:"):
                continue
            addr = e.href[7:].split("?")[0].strip()
            if _PLACEHOLDER_EMAIL.search(addr):
                continue
            if apply_words.search(e.label) or apply_words.search(addr.split("@")[0]) or apply_words.search(e.section):
                return addr
        for m in re.finditer(r"(?:apply|send|mail|email|share)[^.\n]{0,60}?(?:resume|cv|profile)?[^.\n]{0,40}?"
                             r"([\w.+-]+@[\w-]+\.[\w.-]+)", snap.text, re.I):
            if not _PLACEHOLDER_EMAIL.search(m.group(1)):
                return m.group(1)
        return ""

    async def _fallback(self, page: Page, snap: Snapshot, reason: str = "") -> None:
        sig = snap.signature
        if page.url.startswith("chrome-error://") and not self.submitted:
            # the browser's own error page (site down, DNS, certificate): there is nothing here for you to continue
            raise Finish(ApplyResult(ApplyOutcome.FAILED, "The company's site did not load (the browser showed its error "
                                     "page) - it may be down; Retry tries again", ats=self._ats(page),
                                     external_url=self.start_url or page.url))
        if not snap.app_fields and not snap.by_kind(Kind.APPLY, Kind.NEXT, Kind.GUEST):
            gone = UNAVAILABLE_TEXT.search(snap.text[:1500])
            if gone and snap.text_length < 3000 and not self.submitted:
                raise Finish(ApplyResult(ApplyOutcome.EXPIRED, f"Job page unavailable: \"{gone.group(0)}\"",
                                         ats=self._ats(page), external_url=page.url))
            email = self._email_application(snap)
            if email:
                raise Finish(await self._manual(f"{site_label(page.url)} takes applications by email: send your resume "
                                                f"to {email} (the assistant does not send emails for you)"))
        if sig not in self.scrolled:
            self.scrolled.add(sig)
            try:
                await page.evaluate("() => window.scrollTo(0, document.body ? document.body.scrollHeight : 0)")
                await asyncio.sleep(1.0)
                await page.evaluate("() => window.scrollTo(0, 0)")
            except PlaywrightError:
                pass
            self.history.append("scrolled to reveal more content")
            return
        decision = None
        if self.reasoner is not None and self.planner_calls < 8 and await self.reasoner.available():
            self.planner_calls += 1
            await self.ctx.step(f"Asking the local AI what to do on {site_label(page.url)}", page.url)
            goal = (f"Apply to the job '{self.ctx.job.title}' at '{self.ctx.job.company}'. "
                    + ("The application was already submitted; look for confirmation." if self.submitted else ""))
            decision = await self.reasoner.decide(snap, goal, self.history, exclude=self.tried[sig])
            if decision is not None:
                await self.ctx.event("ai_decision", f"AI: {decision.action} "
                                     f"{decision.element.label if decision.element else ''} ({decision.reason})",
                                     page_type=decision.page_type, confidence=decision.confidence)
        if decision is not None and decision.action == "click" and decision.element is not None:
            choice = norm(decision.element.label)
            self.ai_choices[choice] += 1
            if self.ai_choices[choice] > 2:
                # the same advice a third time (limitlessmobil.com: 'Career' eight times, ~15 s and 3 GB of model
                # each): following it again changes nothing - this page is yours
                await self.ctx.event("ai_loop", f"The local AI keeps choosing '{decision.element.label}' "
                                                f"on {site_label(page.url)} - not followed again")
                decision = None
        if decision is not None and decision.action == "click" and decision.element is not None:
            el = decision.element
            if el.kind == Kind.SUBMIT or (el.type == "submit" and el.form):
                await self._submit(page, snap, el, reason="chosen by the local AI")
            else:
                self.tried[sig].add(el.id)
                await self._click_and_record(page, snap, el, f"AI chose '{el.label}': {decision.reason}")
            return
        if decision is not None and decision.action == "stop":
            if decision.page_type == "closed_or_expired":
                raise Finish(ApplyResult(ApplyOutcome.EXPIRED, f"Job looks closed ({decision.reason})",
                                         ats=self._ats(page), external_url=page.url))
            if decision.page_type == "already_applied" and not self.submitted:
                raise Finish(ApplyResult(ApplyOutcome.ALREADY_APPLIED, decision.reason, ats=self._ats(page),
                                         external_url=page.url))
        if decision is not None and decision.action == "wait_for_user":
            await self._human(page, "manual", f"{site_label(page.url)}: {decision.reason or 'please continue the '}"
                                              " application in the browser, then press Continue")
            return
        dead = [label for (url, label), n in self.dead.items() if url == snap.url and n]
        other = " / ".join(dict.fromkeys(snap.other_forms))
        why = reason or (f"clicked '{dead[0]}' but it did not open an application form" if dead
                         else f"found only a {other} form here, not a job application form" if other and not snap.app_fields
                         else "could not tell how to continue")
        await self._human(page, "manual", f"On {site_label(page.url)} the assistant {why}. Please move the application "
                                          "one step forward in the browser (or finish it), then press Continue")

    # ============================================================== acting
    @staticmethod
    def dialog_in_progress(snap: Snapshot) -> bool:
        """An application dialog (fields or its own Next / Submit) is open: everything behind it is off limits."""
        return any(f.dialog for f in snap.app_fields) or any(
            e.dialog and e.kind in {Kind.NEXT, Kind.SUBMIT} for e in snap.elements)

    def _best_button(self, snap: Snapshot, kind: Kind) -> Element | None:
        tried = self.tried[snap.signature]
        in_dialog = self.dialog_in_progress(snap)
        cands = [e for e in snap.by_kind(kind) if e.id not in tried
                 and not self.dead[(snap.url, e.label.lower())] and (e.dialog or not in_dialog)]
        if not cands:
            return None
        title = self.ctx.job.title or ""
        several = len(cands) > 1

        def score(e: Element) -> tuple:
            s = 0.0
            if several and title and e.section and fuzz.token_set_ratio(e.section.lower(), title.lower()) >= 85:
                s += 4  # the Apply button of *this* job on a page listing several
            if e.dialog:
                s += 3
            if not e.ctx:
                s += 2
            if e.tag in {"button", "input"} or e.role == "button":
                s += 1
            if re.search(r"\bapply\b", e.href or "", re.I) or re.search(r"apply|application", e.hint, re.I):
                s += 1
            if re.fullmatch(r"(?:apply(?:\s+now)?|easy apply|quick apply)", e.label.strip(), re.I):
                s += 0.5
            if e.target == "_blank":
                s -= 0.2  # same-tab flows are easier to follow; both work
            return (-s, e.y)
        return sorted(cands, key=score)[0]

    async def _click(self, page: Page, snap: Snapshot, el: Element) -> bool:
        frame = snap.frame(el.frame) if el.frame < len(snap.frames) else page.main_frame
        loc = frame.locator(el.selector).first
        context = page.context
        before_pages = list(context.pages)
        try:
            await self.engine.click(loc, timeout_ms=8000, retries=1)
        except Exception as exc:  # covered element, detached, animation...: last resort DOM click
            log.debug("click failed (%s), trying DOM click", str(exc).splitlines()[0] if str(exc) else exc)
            try:
                await loc.evaluate("el => el.click()")
            except PlaywrightError:
                return False
        await asyncio.sleep(0.8)
        new_pages = [p for p in context.pages if p not in before_pages and not p.is_closed()]
        if new_pages:
            newest = new_pages[-1]
            try:
                await newest.wait_for_load_state("domcontentloaded", timeout=15_000)
            except PlaywrightError:
                pass
            self.own_pages.append(newest)
            self.parents[id(newest)] = page
            self.page = newest
            self.engine.use_page(newest)
            self.history.append(f"opened a new tab: {site_label(newest.url)}")
            await self.ctx.event("new_tab", f"The application continues on {site_label(newest.url)}", url=newest.url)
        return True

    async def _click_and_record(self, page: Page, snap: Snapshot, el: Element, description: str) -> bool:
        """Click, then wait for the page to change. Returns whether anything changed."""
        self.tried[snap.signature].add(el.id)
        await self.ctx.step(description, page.url)
        before = await self.engine.page_signature(page)
        ok = await self._click(page, snap, el)
        self.history.append(description)
        if not ok:
            return False
        active = self._active_page()
        if active is not page:
            return True
        changed = await self.engine.wait_for_page_change(before, timeout_ms=8000, page=page)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10_000)
        except PlaywrightError:
            pass
        if not changed:
            self.dead[(snap.url, el.label.lower())] += 1
            self.history.append(f"'{el.label}' did nothing")
        return changed

    def _active_page(self) -> Page:
        if self.page is not None and not self.page.is_closed():
            return self.page
        opener = self.parents.get(id(self.page)) if self.page is not None else None
        alive = [p for p in self.own_pages if not p.is_closed()]
        if opener is not None and not opener.is_closed():
            self.page = opener  # a sign-in pop-up closed itself: the application is where it was opened from
        elif alive:
            self.page = alive[-1]
        else:
            self.page = self.engine.page
        self.engine.use_page(self.page)
        return self.page

    async def _settle(self, page: Page) -> None:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=15_000)
        except PlaywrightError:
            pass
        try:  # single-page apps keep rendering for a moment after "load"
            await page.wait_for_load_state("networkidle", timeout=2500)
        except PlaywrightError:
            pass

    async def _close_own_pages(self) -> None:
        for p in self.own_pages:
            try:
                if not p.is_closed():
                    await p.close()
            except PlaywrightError:
                pass


async def navigate_application(engine: BrowserEngine, page: Page, ctx: ApplyContext, *, use_ai: bool | None = None,
                               cta_submits: bool = False, ats_name: str | None = None,
                               dialog_only: bool = False) -> ApplyResult:
    """Run the generic navigator on ``page``; returns the application result. ``dialog_only``: the application is
    the dialog that is open on a portal's own page - nothing outside it is read or clicked."""
    settings = get_settings()
    want_ai = settings.navigator_use_ai if use_ai is None else use_ai
    reasoner = PageReasoner() if (want_ai and settings.ai_enabled) else None
    nav = ApplicationNavigator(engine, ctx, reasoner=reasoner, cta_submits=cta_submits, ats_name=ats_name,
                               dialog_only=dialog_only)
    result = await nav.run(page)
    result.ats = result.ats or nav._ats(page)
    if result.outcome == ApplyOutcome.MANUAL_ACTION_REQUIRED and result.external_url and not result.submit_attempted:
        result.resume_url = result.external_url
    result.steps = nav.steps
    return result
