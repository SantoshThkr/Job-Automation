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
from urllib.parse import urlencode, urljoin

from playwright.async_api import Error as PlaywrightError, Locator, Page

from app.answers.question_bank import QuestionContext
from app.browser.errors import ManualActionRequired
from app.browser.selectors import T
from app.engine.context import ApplyContext, GateResult, SubmitDecision
from app.portals.base import PortalAdapter, SearchQuery
from app.portals.common import EXPIRED_TEXT, extract_cards, first_text, all_texts, page_has_success, slugify
from app.portals.types import ApplyOutcome, ApplyResult, JobPosting

log = logging.getLogger(__name__)

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
    def search_url(self, q: SearchQuery, page_no: int) -> str:
        slug = slugify(q.keywords) or "jobs"
        loc = slugify(q.location) if q.location else ""
        path = f"{slug}-jobs" + (f"-in-{loc}" if loc else "") + (f"-{page_no}" if page_no > 1 else "")
        params: dict[str, str | int] = {"k": q.keywords}
        if q.location:
            params["l"] = q.location.lower()
        if q.experience_years is not None:
            params["experience"] = int(q.experience_years)
        if q.posted_within_days:
            params["jobAge"] = q.posted_within_days
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
        job.details_loaded = True
        return job

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
        if key == "expired" or job.extra.get("expired"):
            return ApplyResult(ApplyOutcome.FAILED, "Job is no longer accepting applications")
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

        await ctx.mark_submit_attempt()
        await self.engine.click(button)
        return await self._post_apply(page, job, ctx)

    async def _detect_success(self, page: Page) -> str:
        if await self.engine.exists(SUCCESS, root=page):
            return await self.engine.text_of(SUCCESS, root=page) or "Applied"
        if await self.engine.exists(ALREADY_APPLIED, root=page) and not await self.engine.exists(APPLY_BTN, root=page):
            return "Apply button changed to 'Applied'"
        return await page_has_success(page)

    async def _post_apply(self, page: Page, job: JobPosting, ctx: ApplyContext) -> ApplyResult:
        answered = 0
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
                except ManualActionRequired as exc:
                    return ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED, exc.reason, submit_attempted=True)
                if progressed:
                    answered += 1
                    continue
            if key is None:
                break
        shot = await self.engine.screenshot(f"naukri-unconfirmed-{job.external_id}")
        return ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED,
                           "Clicked Apply but no confirmation was detected - please verify on Naukri",
                           submit_attempted=True, screenshot=shot)

    async def _answer_chatbot_question(self, page: Page, ctx: ApplyContext) -> bool:
        messages = await all_texts(page, BOT_MESSAGES)
        if not messages:
            return False
        question = messages[-1]
        options = await self._chat_options(page)
        field_type = "radio" if options["radio"] else "checkbox_group" if options["checkbox"] else (
            "select" if options["chips"] else "text")
        opts = options["radio"] or options["checkbox"] or options["chips"]
        ans = await ctx.answer(QuestionContext(question=question, field_type=field_type,
                                               options=[o for o, _ in opts], required=True,
                                               job=ctx.job.to_job_input(), portal=self.name))
        before = len(messages)
        if ans.value is None or ans.needs_review:
            res = await ctx.wait_for_human(
                "manual", f"Naukri screening question needs your answer: '{question}'"
                          + (f" (suggested: {ans.value})" if ans.value else ""),
                page=page, detect_done=lambda: self._chat_advanced(page, before))
            if res in (GateResult.RESOLVED, GateResult.APPROVE, GateResult.SUBMITTED):
                return True
            raise ManualActionRequired(f"Naukri screening question needs your answer: '{question}'", kind="form")

        if opts:
            chosen = [v.strip() for v in re.split(r",\s*", ans.value)] if field_type == "checkbox_group" else [ans.value]
            for label, loc in opts:
                if label.strip().lower() in {c.lower() for c in chosen}:
                    await self.engine.click(loc)
        else:
            box = await self.engine.find(CHAT_TEXT_INPUT, timeout_ms=3000)
            if box is None:
                raise ManualActionRequired(f"Could not find the answer box for '{question}'", kind="form")
            await box.click()
            await box.press_sequentially(ans.value, delay=20)
        send = await self.engine.find(CHAT_SEND, timeout_ms=2000)
        if send is not None:
            await self.engine.click(send)
        elif not opts:
            await page.keyboard.press("Enter")
        for _ in range(20):
            if await self._chat_advanced(page, before):
                return True
            await page.wait_for_timeout(300)
        raise ManualActionRequired(f"Naukri did not accept the answer to '{question}'", kind="form")

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


def _id_from_url(url: str) -> str:
    m = re.search(r"-(\d{6,})(?:\?|$|#|\.html)", url or "")
    if m:
        return m.group(1)
    m = re.search(r"jobId=(\d+)", url or "")
    return m.group(1) if m else ""
