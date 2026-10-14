"""Naukri.com adapter.

All Naukri-specific selectors live here. Every target lists several strategies (current hashed
CSS-module classes via attribute fragments, older class names, ids and visible text) so a single
markup change does not break the flow.

Important Naukri behaviour: the "Apply" button *is* the submission (unless a screening chatbot
opens), so Dry Run stops before clicking it and Review mode asks for approval before clicking it.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from urllib.parse import urlencode, urljoin, urlparse

from playwright.async_api import Error as PlaywrightError, Locator, Page

from app.answers.intent import Category, classify
from app.answers.options import choose_option, split_choices
from app.answers.question_bank import Answer, QuestionContext
from app.browser.errors import ManualActionRequired
from app.browser.selectors import T
from app.engine.context import ApplyContext, GateResult, SubmitDecision
from app.engine.submission import SubmissionWatcher, confirmation_since, page_state
from app.portals.base import PortalAdapter, SearchQuery
from app.matching.posting import listing_closed
from app.portals.common import (
    EXPIRED_TEXT,
    all_texts,
    extract_cards,
    first_text,
    generic_apply_button,
    read_page_dates,
    slugify,
)
from app.portals.types import ApplyOutcome, ApplyResult, JobPosting

log = logging.getLogger(__name__)

# the job's own "Posted: 5 days ago" (with the colon) - widgets on the same page say "Posted 30+ days ago"
_POSTED_STAT = re.compile(r"\bPosted:\s*([^\n|]{2,40}?\bago\b|just now|today|few hours ago)", re.I)

# ------------------------------------------------------------------ selectors

LOGGED_IN = T("naukri logged in", css=[".nI-gNb-drawer__icon", ".nI-gNb-info", ".view-profile-wrapper",
                                      "a[href*='/mnjuser/profile']", "img.nI-gNb-icon-img", "[class*='user-name']"])
LOGGED_OUT = T("naukri login button", css=["#login_Layer", "a[title='Jobseeker Login']", "#usernameField",
                                           "form[name='login-form']"], text=["Login"])

CARD_SELECTORS = ["div.srp-jobtuple-wrapper", "div.cust-job-tuple", "article.jobTuple", "div[data-job-id]",
                  "div[class*='jobTuple']"]
CARD_FIELDS = {
    "title": ["a.title", "a[class*='title']", "h2 a", ".row1 a"],
    "company": ["a.comp-name", "a[class*='comp-name']", "a.subTitle", "span.comp-name", "[class*='companyInfo'] a"],
    "experience": ["span.expwdth", ".exp-wrap span[title]", ".exp-wrap", "li.experience span", "[class*='exp'] span"],
    "salary": [".sal-wrap span[title]", ".sal-wrap", "li.salary span", "[class*='sal'] span"],
    "location": ["span.locWdth", ".loc-wrap span[title]", ".loc-wrap", "li.location span", "[class*='loc'] span"],
    "snippet": ["span.job-desc", "div.job-desc", "div.job-description", "[class*='job-desc']"],
    "posted": ["span.job-post-day", "div.type span", "[class*='post-day']", "[class*='posted']"],
}
CARD_MULTI = {"skills": ["ul.tags-gt li", "ul.tags li", "ul[class*='tags'] li"]}
CARD_ATTRS = {"href": (["a.title", "a[class*='title']", "h2 a"], "href"), "job_id": ([":self"], "data-job-id")}
CARD_FLAGS = {"applied": [".applied", "[class*='applied']", "span.already-applied"]}
NO_RESULTS = T("no results", css=["[class*='no-result']", ".noResultContainer", "#no-result"],
               within_text=["No results found"])

DETAIL_TITLE = ["h1[class*='jd-header-title']", "h1.jd-header-title", "header h1", "h1"]
DETAIL_COMPANY = ["[class*='jd-header-comp-name'] a", "[class*='jd-header-comp-name']", "a.pad-rt-8", ".jd-header-comp-name"]
DETAIL_EXP = ["[class*='jhc__exp'] span", "[class*='jhc__exp']", ".exp span"]
DETAIL_SALARY = ["[class*='jhc__salary'] span", "[class*='jhc__salary']", ".salary span"]
DETAIL_LOCATION = ["[class*='jhc__location']", "[class*='jhc__loc']", ".location"]
DETAIL_JD = ["[class*='JDC__dang-inner-html']", "div.dang-inner-html", "[class*='job-desc-container']",
             "section[class*='job-desc']", "section.job-desc", "#job_description", "[class*='job-desc']"]
DETAIL_SKILLS = ["[class*='key-skill'] a", "[class*='key-skill'] span", "a[class*='chip']", ".key-skill a"]

APPLY_BTN = T("naukri apply", css=["button#apply-button", "button.apply-button", "button[class*='apply-button']",
                                   ".apply-button-container button:not([id*='company'])"],
              role=("button", r"^\s*apply\s*$"), text=["Apply"])
COMPANY_SITE_BTN = T("apply on company site", css=["button#company-site-button", "button[class*='company-site']",
                                                    "a[class*='company-site']"],
                     role=("button", r"apply\s+on\s+company\s+site"), text=["Apply on company site"])
ALREADY_APPLIED = T("already applied", css=["#already-applied", "button[class*='already-applied']",
                                            "span[class*='already-applied']"],
                    role=("button", r"^\s*applied\s*$"), text=["Applied"])
EXPIRED = T("expired", css=["[class*='expired']", "[class*='job-closed']"],
            within_text=["no longer accepting applications", "This job has expired"])

SUCCESS = T("apply success", css=["span.apply-message", "[class*='apply-message']", "[class*='applied-success']",
                                  "[class*='apply-status-header'][class*='success']"],
            within_text=["successfully applied", "Applied successfully"])
CHATBOT = T("chatbot drawer", css=["div.chatbot_DrawerContentWrapper", "div[class*='chatbot_Drawer']",
                                   "div[class*='chatbot_MessageContainer']", "#chatbot", "div[class*='chatbot']"])
BOT_MESSAGES = ["li.botItem div span", "li.botItem", "div.botMsg", "[class*='botMsg']", "[class*='bot-msg']"]
CHAT_TEXT_INPUT = T("chat input", css=["div.textArea[contenteditable='true']", "div[contenteditable='true'][class*='textArea']",
                                       "[class*='chatbot_InputContainer'] [contenteditable='true']",
                                       "[class*='chatbot'] input[type='text']", "[class*='chatbot'] textarea"])
CHAT_RADIOS = ["div.ssrc__radio-btn-container", "[class*='radio-btn-container']", "[class*='chatbot'] label:has(input[type='radio'])"]
CHAT_CHECKBOXES = ["div.mcc__checkbox-container", "[class*='checkbox-container']", "[class*='chatbot'] label:has(input[type='checkbox'])"]
CHAT_CHIPS = ["div.chatbot_Chip", "[class*='chatbot_Chip']", "[class*='chip-container'] span"]
CHAT_SEND = T("chat save", css=["div.sendMsg", "div.send", "[class*='sendMsg']", "[class*='chatbot'] button[type='submit']"],
              text=["Save", "Send", "Submit"])



# text Naukri shows (or answers with) when it does not take an application
_REFUSAL_TEXT = re.compile(r"limit|quota|exhaust|exceed|maximum|too\s+many|try\s+again|unable|cannot|can.?t\b|could\s+not|"
                           r"\berror\b|failed|not\s+allowed|your\s+profile|verify|blocked|restricted|suspicious|"
                           r"no\s+longer|expired|something\s+went\s+wrong", re.I)
# ...because of a quota / rate limit: the job is fine, the portal is not taking more applications for now
_LIMIT_TEXT = re.compile(r"(?:daily|apply|application)\s+limit|limit\s+(?:is\s+|has\s+been\s+)?(?:reached|exceeded|exhausted|over)|"
                         r"\bquota\b|exhausted|exceeded\s+(?:the|your)|maximum\s+(?:number\s+of\s+)?(?:job\s+)?appl|"
                         r"too\s+many\s+(?:requests|applications|attempts)|try\s+(?:again\s+)?(?:after\s+some\s+time|"
                         r"later|tomorrow|after\s+\d+)", re.I)

class NaukriAdapter(PortalAdapter):
    name = "naukri"
    display_name = "Naukri"

    @property
    def login_url(self) -> str:
        return f"{self.base_url}/nlogin/login"

    @property
    def home_url(self) -> str:
        return f"{self.base_url}/mnjuser/homepage"

    async def is_logged_in(self, navigate: bool = True) -> bool:
        page = self.engine.page
        if navigate:
            await self.engine.goto(self.home_url)
        url = page.url.lower()
        if "nlogin" in url or "/login" in url:
            return False
        key, _ = await self.engine.wait_for_any({"in": LOGGED_IN, "out": LOGGED_OUT},
                                                timeout_ms=8000 if navigate else 500)
        return key == "in"

    async def _check_session(self) -> None:
        url = self.engine.page.url.lower()
        if "nlogin" in url:
            from app.browser.errors import LoginRequired

            raise LoginRequired(self.name, "Naukri session expired (redirected to login)")

    # ------------------------------------------------------------ search
    @staticmethod
    def job_age(days: int | None) -> int | None:
        """Naukri's "freshness" filter only knows 1, 3, 7, 15 and 30 days - any other value (the profile's 2) was
        silently ignored, and three weeks old and older jobs were listed. The smallest one that covers ``days``;
        none beyond 30 (every posting is then checked on its own date)."""
        if not days:
            return None
        return next((d for d in (1, 3, 7, 15, 30) if d >= days), None)

    def search_url(self, q: SearchQuery, page_no: int) -> str:
        slug = slugify(q.keywords) or "jobs"
        loc = slugify(q.location) if q.location else ""
        path = f"{slug}-jobs" + (f"-in-{loc}" if loc else "") + (f"-{page_no}" if page_no > 1 else "")
        params: dict[str, str | int] = {"k": q.keywords}
        if q.location:
            params["l"] = q.location.lower()
        if q.experience_years is not None:
            params["experience"] = int(q.experience_years)
        if (age := self.job_age(q.posted_within_days)) is not None:
            params["jobAge"] = age
        return f"{self.base_url}/{path}?{urlencode(params)}"

    async def search(self, query: SearchQuery) -> AsyncIterator[JobPosting]:  # type: ignore[override]
        seen: set[str] = set()
        yielded = 0
        for page_no in range(1, max(1, query.max_pages) + 1):
            await self.engine.goto(self.search_url(query, page_no))
            await self._check_session()
            key, _ = await self.engine.wait_for_any(
                {"cards": T(css=CARD_SELECTORS), "none": NO_RESULTS}, timeout_ms=15_000)
            if key != "cards":
                log.info("Naukri: no results on page %d for %r", page_no, query.keywords)
                break
            await self.engine.scroll_to_load_all(T(css=CARD_SELECTORS), max_rounds=4)
            cards = await extract_cards(self.engine.page, CARD_SELECTORS, CARD_FIELDS, CARD_MULTI, CARD_ATTRS, CARD_FLAGS)
            new = 0
            for c in cards:
                job = self._card_to_job(c)
                if job is None or job.external_id in seen:
                    continue
                seen.add(job.external_id)
                new += 1
                yielded += 1
                yield job
                if yielded >= query.max_jobs:
                    return
            if new == 0:
                break

    def _card_to_job(self, c: dict) -> JobPosting | None:
        href = c.get("href") or ""
        url = urljoin(self.base_url + "/", href) if href else ""
        job_id = c.get("job_id") or _id_from_url(url)
        if not job_id or not c.get("title"):
            return None
        return JobPosting(portal=self.name, external_id=str(job_id), url=url, title=c["title"],
                          company=c.get("company", ""), location=c.get("location", ""),
                          experience_text=c.get("experience", ""), salary_text=c.get("salary", ""),
                          posted_text=c.get("posted", ""), snippet=c.get("snippet", ""),
                          skills=c.get("skills") or [], already_applied=bool(c.get("applied")))

    # ----------------------------------------------------------- details
    async def fetch_details(self, job: JobPosting) -> JobPosting:
        await self.engine.goto(job.url)
        await self._check_session()
        page = self.engine.page
        await self.engine.wait_for_any({"jd": T(css=DETAIL_JD), "title": T(css=DETAIL_TITLE[:2]), "expired": EXPIRED},
                                       timeout_ms=15_000)
        if await self._expired_redirect(page, job):
            job.extra["expired"] = True
            job.details_loaded = True
            return job  # Naukri showed search results instead: keep the listing's own title/company
        job.title = await first_text(page, DETAIL_TITLE) or job.title
        job.company = (await first_text(page, DETAIL_COMPANY)).split("\n")[0] or job.company
        job.experience_text = await first_text(page, DETAIL_EXP) or job.experience_text
        job.salary_text = await first_text(page, DETAIL_SALARY) or job.salary_text
        job.location = await first_text(page, DETAIL_LOCATION) or job.location
        jd = await first_text(page, DETAIL_JD)
        if jd:
            job.description = jd
        skills = await all_texts(page, DETAIL_SKILLS)
        if skills:
            job.skills = list(dict.fromkeys([*job.skills, *skills]))
        key, _ = await self.engine.wait_for_any(
            {"applied": ALREADY_APPLIED, "external": COMPANY_SITE_BTN, "apply": APPLY_BTN}, timeout_ms=4000)
        if key == "applied":
            job.already_applied = True
        elif key == "external":
            job.apply_type = "external"
        elif key == "apply":
            job.apply_type = "portal"
        body = await self.engine.body_text(limit=6000)
        if EXPIRED_TEXT.search(body):
            job.extra["expired"] = True
        await read_page_dates(page, job)  # (JSON-LD: "3+ weeks ago" was 2026-06-25 on one page)
        if job.posted_at is None and (stat := _POSTED_STAT.search(body)):
            job.posted_text = stat.group(1).strip()  # the job's own "Posted: 5 days ago", not a widget's "Posted 30+ days ago"
        closed = listing_closed(job)
        if closed:
            job.extra["expired"], job.extra["expired_reason"] = True, f"Closed - {closed}"
        job.details_loaded = True
        return job

    async def _expired_redirect(self, page: Page, job: JobPosting) -> bool:
        """Expired Naukri jobs redirect to search results with a 'Job you are looking for is expired' banner."""
        body = await self.engine.body_text(page, limit=3000)
        if not EXPIRED_TEXT.search(body):
            return False
        on_job_page = bool(_id_from_url(page.url)) and job.external_id in page.url
        return not on_job_page or not await first_text(page, DETAIL_JD)

    # ------------------------------------------------------------- apply
    async def apply(self, job: JobPosting, ctx: ApplyContext) -> ApplyResult:
        page = self.engine.page
        if _id_from_url(page.url) != job.external_id and job.url not in page.url:
            await self.engine.goto(job.url)
            await self._check_session()
        key, button = await self.engine.wait_for_any(
            {"applied": ALREADY_APPLIED, "external": COMPANY_SITE_BTN, "apply": APPLY_BTN, "expired": EXPIRED},
            timeout_ms=10_000)
        if key == "applied":
            return ApplyResult(ApplyOutcome.ALREADY_APPLIED, "Naukri shows this job as already applied")
        if key == "expired" or job.extra.get("expired") or await self._expired_redirect(page, job):
            return ApplyResult(ApplyOutcome.EXPIRED, "Job is no longer accepting applications")
        if key is None:
            found = await generic_apply_button(page)  # markup changed: find the button by what it says
            if found is not None:
                key, button = found
                await ctx.event("generic_button", f"Found the Naukri {key} button by its text")
        if key == "external":
            await ctx.event("external", "Naukri redirects to the company website")
            assert button is not None
            before = page.url
            new_page = await self.engine.expect_popup(lambda: self.engine.click(button), timeout_ms=10_000)
            target = new_page or page
            if new_page is None:
                await self.engine.wait_for_page_change(await self.engine.page_signature(), timeout_ms=5000)
                if page.url == before:
                    return ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED,
                                       "Clicked 'Apply on company site' but no external page opened")
            return ApplyResult(ApplyOutcome.EXTERNAL, "External application", external_url=target.url,
                               external_page=target)
        if key != "apply" or button is None:
            shot = await self.engine.screenshot(f"naukri-no-apply-{job.external_id}")
            return ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED, "Could not find the Apply button", screenshot=shot)

        decision = await ctx.decide_submit(
            page, unresolved=[], summary=f"Apply to '{job.title}' at {job.company} on Naukri (Naukri applies on click)",
            detect_submitted=lambda: self._detect_success(page))
        if decision == SubmitDecision.DRY_RUN:
            return ApplyResult(ApplyOutcome.DRY_RUN, "Dry run: would click Apply on Naukri")
        if decision == SubmitDecision.REJECTED:
            return ApplyResult(ApplyOutcome.REJECTED, "You rejected this application")
        if decision == SubmitDecision.MANUAL:
            return ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED, "Not submitted - needs your review")
        if decision == SubmitDecision.SUBMITTED_BY_USER:
            text = await self._detect_success(page)
            return ApplyResult(ApplyOutcome.APPLIED if text else ApplyOutcome.MANUAL_ACTION_REQUIRED,
                               "Submitted by you in the browser", confirmation_text=text, submit_attempted=True)

        await self.engine.wait_for_load("load", timeout=10_000, page=page)  # a click before the page is interactive is lost
        self._before = await page_state(page)  # what the page already said (job description...) never confirms
        await ctx.mark_submit_attempt()
        # what Naukri answers to the click: its apply request, and any text it shows (also a toast that is gone again)
        self._apply_watch = SubmissionWatcher(page)
        await self._apply_watch.begin()
        await self._record_new_text(page)
        try:
            await self.engine.click(button)
            res = await self._post_apply(page, job, ctx)
        finally:
            self._apply_watch.stop()
        if res.outcome == ApplyOutcome.APPLIED:
            res.verification, res.evidence = "confirmed", [f"portal_confirmation: {res.confirmation_text[:160]}"]
        elif res.submit_attempted and res.outcome == ApplyOutcome.MANUAL_ACTION_REQUIRED and \
                "not verified" in res.message:
            res.verification = "uncertain"
        return res

    async def _detect_success(self, page: Page) -> str:
        if await self.engine.exists(SUCCESS, root=page):
            return await self.engine.text_of(SUCCESS, root=page) or "Applied"
        if await self.engine.exists(ALREADY_APPLIED, root=page) and not await self.engine.exists(APPLY_BTN, root=page):
            return "Apply button changed to 'Applied'"
        before = getattr(self, "_before", None)
        return await confirmation_since(page, before) if before is not None else ""

    async def _post_apply(self, page: Page, job: JobPosting, ctx: ApplyContext) -> ApplyResult:
        answered = 0
        reclicked = False
        for _ in range(25):
            key, _el = await self.engine.wait_for_any(
                {"success": SUCCESS, "chatbot": CHATBOT, "login": LOGGED_OUT}, timeout_ms=10_000)
            text = await self._detect_success(page)
            if text:
                return ApplyResult(ApplyOutcome.APPLIED, f"Applied on Naukri ({answered} question(s) answered)",
                                   confirmation_text=text, submit_attempted=True)
            if key == "login":
                return ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED, "Naukri asked to log in during apply",
                                   submit_attempted=True)
            blockers = await self.engine.detect_blockers(page)
            if blockers:
                res = await ctx.wait_for_human(blockers[0], f"Naukri shows a {blockers[0].upper()} - please complete it",
                                               page=page, detect_done=lambda: self._detect_success(page))
                if res in (GateResult.RESOLVED, GateResult.SUBMITTED, GateResult.APPROVE):
                    continue
                return ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED, f"{blockers[0]} required", submit_attempted=True)
            if key == "chatbot":
                try:
                    progressed = await self._answer_chatbot_question(page, ctx)
                except ChatFinished as done:
                    await ctx.event("chatbot", f"Screening chat finished: '{done}'")
                    break
                except ManualActionRequired as exc:
                    # The screening chat stopped on a question. Naukri registers the application only when the chat
                    # is completed: when the reloaded job still offers "Apply", nothing was sent - and the job can be
                    # applied to again by itself once the answer is known (your profile, a saved answer).
                    unsent = False
                    if ctx.mode.value != "dry_run":
                        try:
                            await self.engine.goto(job.url)
                            state, _ = await self.engine.wait_for_any({"applied": ALREADY_APPLIED, "apply": APPLY_BTN},
                                                                      timeout_ms=8000)
                            unsent = state == "apply"
                        except Exception as reload_exc:  # noqa: BLE001 - could not check: keep the cautious record
                            log.debug("could not reload the job after the chat stopped: %s", reload_exc)
                    return ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED, exc.reason, submit_attempted=not unsent,
                                       nothing_sent=unsent,
                                       evidence=["after reloading, Naukri still offers 'Apply': nothing was sent"] if unsent else [])
                if progressed:
                    answered += 1
                    continue
            if key is None and not answered and not reclicked:
                again = await self._unchanged_apply_button(page)
                if again is not None:  # nothing at all happened: the click was lost, not refused
                    reclicked = True
                    await ctx.event("apply_retry", "Naukri did not react to the Apply click - clicking it again")
                    await self.engine.click(again)
                    continue
            if key is None:
                break
        confirmed = await self._confirm_applied(page, job)
        if confirmed:
            return ApplyResult(ApplyOutcome.APPLIED, f"Applied on Naukri ({answered} question(s) answered)",
                               confirmation_text=confirmed, submit_attempted=True)
        shot = await self.engine.screenshot(f"naukri-unconfirmed-{job.external_id}")
        if self._still_unapplied and not answered:
            said, evidence, limit = self._apply_refusal
            await ctx.event("apply_not_registered", "Naukri did not register the application"
                            + (f": {said}" if said else " and gave no reason"), evidence=evidence)
            if limit:
                return ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED,
                                   f"Naukri is not accepting more applications right now ({said}) - nothing was sent",
                                   submit_attempted=False, screenshot=shot, evidence=evidence, portal_limit=said,
                                   nothing_sent=True, click_ignored=True)
            return ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED,
                               "Naukri did not register the Apply click (after reloading, the job still shows 'Apply') - "
                               "nothing was sent" + (f". Naukri said: {said}" if said else "") + "; Retry applies again",
                               submit_attempted=False, screenshot=shot, evidence=evidence, nothing_sent=True,
                               click_ignored=True)
        return ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED,
                           "Submitted - confirmation not verified: clicked Apply, but Naukri showed neither its success "
                           "message nor 'Applied' (also after reloading the job)", submit_attempted=True, screenshot=shot,
                           verification="uncertain", evidence=["apply_clicked", "no portal confirmation after reload"])

    async def _unchanged_apply_button(self, page: Page):
        if await self.engine.exists(ALREADY_APPLIED, root=page) or await self.engine.exists(CHATBOT, root=page):
            return None
        btn = await self.engine.find(APPLY_BTN, root=page)
        if btn is None:
            return None
        try:
            label = (await btn.inner_text()).strip().lower()
            enabled = await btn.is_enabled()
        except PlaywrightError:
            return None
        return btn if enabled and label in {"apply", "apply now"} else None

    _NEW_TEXT_JS = r"""
    () => {
      if (window.__pjaNewText) return;
      window.__pjaNewText = [];
      const keep = (t) => { t = (t || '').replace(/\s+/g, ' ').trim();
        if (t.length >= 8 && t.length <= 240 && !window.__pjaNewText.includes(t) && window.__pjaNewText.length < 40) window.__pjaNewText.push(t); };
      new MutationObserver((records) => { for (const r of records) {
        for (const n of r.addedNodes) { if (n.nodeType === 1 && !/^(SCRIPT|STYLE|LINK|IFRAME|IMG|SVG|PATH)$/i.test(n.tagName)) keep(n.innerText); }
        if (r.type === 'characterData' && r.target.parentElement) keep(r.target.parentElement.innerText);
      } }).observe(document.body, { childList: true, subtree: true, characterData: true });
    }
    """

    async def _record_new_text(self, page: Page) -> None:
        try:
            await page.evaluate(self._NEW_TEXT_JS)
        except PlaywrightError:
            pass

    async def _read_apply_refusal(self, page: Page) -> tuple[str, list[str], bool]:
        """(what Naukri said, evidence, is it a quota / rate limit?) for an Apply click that changed nothing."""
        watch = getattr(self, "_apply_watch", None)
        evidence: list[str] = []
        said = ""
        limit = False
        hits = list(watch.network) if watch is not None else []
        for h in hits:
            where = urlparse(h.url).path.rstrip("/").rsplit("/", 2)[-1][:40] or urlparse(h.url).netloc
            evidence.append(f"request: {h.method} …/{where} -> {h.status}" + (f" \"{h.message[:120]}\"" if h.message else ""))
            if (h.status >= 400 or h.failure) and h.message and not said:
                said = h.message
            limit = limit or h.status == 429
        try:
            texts = await page.evaluate("() => window.__pjaNewText || []")
        except PlaywrightError:
            texts = []
        shown = [t for t in texts if _REFUSAL_TEXT.search(t)]
        for t in shown[:3]:
            evidence.append(f"shown: {t[:160]}")
        said = said or (shown[0] if shown else "")
        if not hits and not texts:
            evidence.append("no request was sent and nothing appeared on the page")
        limit = limit or bool(_LIMIT_TEXT.search(" ".join([said, *shown])))
        return said[:200], evidence[:8], limit

    async def _confirm_applied(self, page: Page, job: JobPosting) -> str:
        """No success banner seen: wait a little, then reload the job - Naukri shows 'Applied' once it went through."""
        self._still_unapplied = False
        self._apply_refusal: tuple[str, list[str], bool] = ("", [], False)
        for _ in range(10):
            text = await self._detect_success(page)
            if text:
                return text
            await page.wait_for_timeout(500)
        self._apply_refusal = await self._read_apply_refusal(page)  # before the reload wipes the page
        try:
            await self.engine.goto(job.url)
            key, _ = await self.engine.wait_for_any({"applied": ALREADY_APPLIED, "apply": APPLY_BTN}, timeout_ms=8000)
        except Exception as exc:  # navigation problems: leave it for you to verify
            log.debug("could not reload the job to confirm: %s", exc)
            return ""
        self._still_unapplied = key == "apply"
        return "Naukri now shows this job as Applied" if key == "applied" else ""

    async def _answer_chatbot_question(self, page: Page, ctx: ApplyContext) -> bool:
        messages = await all_texts(page, BOT_MESSAGES)
        if not messages:
            return False
        question = messages[-1]
        before = len(messages)
        if classify(question).category == Category.NOT_A_QUESTION:
            # a greeting / instruction / thank-you, not a question: wait for what comes next
            if await self._wait_chat(page, before, 8):
                return True
            raise ChatFinished(question)
        opts, text_box, send = await self._chat_controls(page, question)
        field_type = "radio" if opts else "text"
        if opts and any(kind == "checkbox" for _l, _loc, kind in opts):
            field_type = "checkbox_group"
        labels = [label for label, _loc, _k in opts]
        qctx = QuestionContext(question=question, field_type=field_type, options=labels, required=True,
                               job=ctx.job.to_job_input(), portal=self.name)
        ans = await ctx.answer(qctx)
        if ans.value is None or ans.needs_review:
            await ctx.log_question(question, ans, filled=False, verified=None)
            # ask you once in the dashboard (or answer in the chat yourself); your answer is typed in and remembered
            answers = await ctx.ask_questions(
                [{"id": "naukri-chatbot", "question": question, "type": field_type, "options": labels,
                  "required": True, "suggested": ans.value or "", "note": "Naukri screening chatbot"}],
                page, intro="Naukri screening question", detect_done=lambda: self._chat_advanced(page, before))
            value = (answers or {}).get("naukri-chatbot", "").strip()
            if answers is not None and not value:
                if await self._chat_advanced(page, before):
                    return True  # you answered directly in the chat
                raise ManualActionRequired(f"Naukri screening question was not answered: '{question}'", kind="form")
            if not value:
                raise ManualActionRequired(f"Naukri screening question needs your answer: '{question}'", kind="form")
            if opts and field_type != "checkbox_group":
                value = choose_option(value, labels) or value
            await ctx.save_user_answer(question, value)
            ans = Answer(value, 1.0, "user", category=ans.category, mode="user", sources=["you"])
            if ctx.record_answer is not None:
                await ctx.record_answer(None, qctx, ans)

        if opts:
            chosen = (split_choices(ans.value, [label for label, _loc, _kind in opts]) if field_type == "checkbox_group"
                      else [ans.value])
            want = {c.lower() for c in chosen}
            clicked = False
            for label, loc, _kind in opts:
                if label.strip().lower() in want:
                    await self._click_option(loc)
                    clicked = True
            if not clicked:
                raise ManualActionRequired(f"Could not select '{ans.value}' for '{question}'", kind="form")
        elif text_box is not None:
            await text_box.click()
            try:
                await text_box.fill("")
            except PlaywrightError:
                pass
            await text_box.press_sequentially(ans.value, delay=20)
            await self._pick_suggestion(page, text_box, ans.value)
        else:
            raise ManualActionRequired(f"Could not find the answer box for '{question}'", kind="form")
        if send is not None:
            await self.engine.click(send)
        elif not opts:
            await page.keyboard.press("Enter")
        advanced = await self._wait_chat(page, before, 6)
        await ctx.log_question(question, ans, filled=True, verified=advanced,
                               problem="" if advanced else "the chat did not move on")
        if advanced:
            return True
        raise ManualActionRequired(f"Naukri did not accept the answer to '{question}'", kind="form")

    async def _wait_chat(self, page: Page, before: int, seconds: float) -> bool:
        for _ in range(int(seconds / 0.3)):
            if await self._chat_advanced(page, before):
                return True
            await page.wait_for_timeout(300)
        return False

    async def _chat_controls(self, page: Page, question: str):
        """Answer controls for the latest question: Naukri's known widgets first, then whatever the chat shows."""
        options = await self._chat_options(page)
        known = [(label, loc, kind) for kind in ("radio", "checkbox", "chips") for label, loc in options[kind]]
        text_box = None if known else await self.engine.find(CHAT_TEXT_INPUT, timeout_ms=1500)
        send = await self.engine.find(CHAT_SEND, timeout_ms=800)
        if known or text_box is not None:
            return known, text_box, send
        try:
            found = await page.evaluate(_GENERIC_CHAT_JS, question)
        except PlaywrightError:
            found = None
        if not found:
            return [], None, send
        opts = [(o["text"], page.locator(f'[data-pja-chat="{o["id"]}"]').first, o["kind"]) for o in found["options"]]
        box = None
        if not opts and found["inputs"]:
            box = page.locator(f'[data-pja-chat="{found["inputs"][0]["id"]}"]').first
        if send is None and found.get("send"):
            send = page.locator(f'[data-pja-chat="{found["send"]}"]').first
        return opts, box, send

    async def _click_option(self, loc: Locator) -> None:
        try:
            tag = await loc.evaluate("e => e.tagName + ':' + (e.type || '')")
            if tag in {"INPUT:radio", "INPUT:checkbox"}:
                try:
                    await loc.check(timeout=3000)
                    return
                except PlaywrightError:
                    await loc.evaluate("e => { const l = (e.id && document.querySelector('label[for=\"' + e.id + '\"]'))"
                                       " || e.closest('label'); (l || e).click(); }")
                    return
        except PlaywrightError:
            pass
        await self.engine.click(loc)

    async def _pick_suggestion(self, page: Page, box: Locator, value: str) -> None:
        """Typeahead answers (city, skills...): pick the matching suggestion when the chat offers a list."""
        await page.wait_for_timeout(700)
        sugg = page.locator("[role='option']:visible, [role='listbox'] li:visible, [class*='suggest' i] li:visible, "
                            "[class*='suggest' i] div:visible")
        try:
            n = min(await sugg.count(), 20)
            texts = [((await sugg.nth(i).inner_text()) or "").strip() for i in range(n)]
        except PlaywrightError:
            return
        picked = choose_option(value, [t for t in texts if t], min_score=75) if texts else None
        if picked:
            await sugg.nth(texts.index(picked)).click()

    async def _chat_advanced(self, page: Page, before: int) -> bool:
        if await self._detect_success(page):
            return True
        return len(await all_texts(page, BOT_MESSAGES)) > before

    async def _chat_options(self, page: Page) -> dict[str, list[tuple[str, Locator]]]:
        out: dict[str, list[tuple[str, Locator]]] = {"radio": [], "checkbox": [], "chips": []}
        for kind, sels in (("radio", CHAT_RADIOS), ("checkbox", CHAT_CHECKBOXES), ("chips", CHAT_CHIPS)):
            for sel in sels:
                loc = page.locator(sel)
                try:
                    n = await loc.count()
                except PlaywrightError:
                    continue
                items = []
                for i in range(n):
                    el = loc.nth(i)
                    try:
                        if not await el.is_visible():
                            continue
                        # click the label (or the input) - option containers can be wider than their text
                        target = el
                        for inner in ("label", "input"):
                            cand = el.locator(inner)
                            if await cand.count() and await cand.first.is_visible():
                                target = cand.first
                                break
                        items.append(((await el.inner_text()).strip(), target))
                    except PlaywrightError:
                        continue
                if items:
                    out[kind] = items
                    break
        return out


class ChatFinished(Exception):
    """The screening chatbot ended (e.g. 'Thank you for your responses.') without a question."""


_GENERIC_CHAT_JS = r"""
(lastText) => {
  const T = (e) => (e.innerText || e.textContent || '').replace(/\s+/g, ' ').trim();
  const shown = (e) => !!e && e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden';
  const all = Array.from(document.querySelectorAll('li, p, span, div')).filter(e => shown(e) && T(e) === lastText);
  const anchor = all.length ? all[all.length - 1] : null;
  if (!anchor) return null;
  const CTRL = 'input:not([type=hidden]), textarea, select, [contenteditable="true"], [role="radio"], [role="checkbox"], [role="option"], [style*="cursor: pointer"], [style*="cursor:pointer"]';
  let box = anchor;
  for (let d = 0; d < 10 && box.parentElement && box !== document.body; d++) {
    box = box.parentElement;
    if (Array.from(box.querySelectorAll(CTRL)).some(e => e !== anchor && !anchor.contains(e))) break;
  }
  const after = (e) => !!(anchor.compareDocumentPosition(e) & Node.DOCUMENT_POSITION_FOLLOWING) && !anchor.contains(e);
  const tag = (e) => { if (!e.dataset.pjaChat) { window.__pjaChat = (window.__pjaChat || 0) + 1; e.dataset.pjaChat = 'c' + window.__pjaChat; } return e.dataset.pjaChat; };
  const labelOf = (inp) => { const l = (inp.id && document.querySelector('label[for="' + CSS.escape(inp.id) + '"]')) || inp.closest('label'); return l ? T(l) : (inp.value || inp.getAttribute('aria-label') || ''); };
  const isMsg = (e) => !!e.closest('li[class*="bot" i], li[class*="user" i], [class*="botItem" i], [class*="userItem" i], [class*="botMsg" i]');
  const SEND = /^(save|send|submit|next|done|skip)$/i;
  const options = []; const seen = new Set();
  box.querySelectorAll('input[type=radio], input[type=checkbox], [role=radio], [role=checkbox], [role=option]').forEach(e => {
    if (!after(e) || isMsg(e)) return;
    if (!(shown(e) || shown(e.closest('label')) || shown(e.parentElement))) return;
    const text = e.tagName === 'INPUT' ? labelOf(e) : T(e);
    if (!text || text.length > 80 || seen.has(text)) return;
    seen.add(text);
    options.push({ id: tag(e), text, kind: e.type === 'checkbox' || e.getAttribute('role') === 'checkbox' ? 'checkbox' : 'radio' });
  });
  if (!options.length) {
    box.querySelectorAll('div, span, button, li, a, label').forEach(e => {
      if (!after(e) || !shown(e) || isMsg(e) || e.querySelector('input, textarea, [contenteditable="true"]')) return;
      const text = T(e);
      if (!text || text.length > 60 || seen.has(text) || SEND.test(text)) return;
      if (Array.from(e.children).some(c => T(c) === text)) return;
      const clickable = getComputedStyle(e).cursor === 'pointer' || e.tagName === 'BUTTON' || e.getAttribute('role');
      if (!clickable) return;
      seen.add(text);
      options.push({ id: tag(e), text, kind: 'chip' });
    });
  }
  const inputs = [];
  box.querySelectorAll('input:not([type=hidden]):not([type=radio]):not([type=checkbox]), textarea, [contenteditable="true"], select').forEach(e => {
    if (shown(e)) inputs.push({ id: tag(e), kind: e.tagName === 'SELECT' ? 'select' : (e.isContentEditable ? 'contenteditable' : 'text') });
  });
  const sendEl = Array.from(box.querySelectorAll('button, [role=button], div, span')).find(e => shown(e) && after(e)
      && SEND.test(T(e) || e.getAttribute('aria-label') || '') && !/^skip$/i.test(T(e))
      && !Array.from(e.children).some(c => T(c) === T(e)));  // the innermost element, not its wrapper
  return { options, inputs, send: sendEl ? tag(sendEl) : null };
}
"""


def _id_from_url(url: str) -> str:
    m = re.search(r"-(\d{6,})(?:\?|$|#|\.html)", url or "")
    if m:
        return m.group(1)
    m = re.search(r"jobId=(\d+)", url or "")
    return m.group(1) if m else ""
