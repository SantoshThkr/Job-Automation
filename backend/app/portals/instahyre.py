"""Instahyre adapter: matched opportunities + skill search, job details, apply (one-click or modal).

All Instahyre-specific selectors live here. Instahyre's "Apply"/"Interested" button may submit
immediately, so the submit gate (dry run / review / auto) runs *before* that first click; an
optional follow-up modal (message, CTC, questions) is then filled and confirmed.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from urllib.parse import urlencode, urljoin

from playwright.async_api import Error as PlaywrightError, Page

from app.browser.errors import LoginRequired
from app.browser.forms import collect_errors, summarize_unresolved
from app.browser.selectors import T
from app.engine.context import ApplyContext, SubmitDecision
from app.engine.questions import ask_and_fill
from app.engine.submission import confirmation_since, page_state
from app.portals.base import PortalAdapter, SearchQuery
from app.portals.common import EXPIRED_TEXT, all_texts, extract_cards, first_text, generic_apply_button, read_page_dates
from app.portals.types import ApplyOutcome, ApplyResult, JobPosting

log = logging.getLogger(__name__)

LOGGED_IN = T("instahyre logged in", css=["a[href*='/candidate/profile']", ".candidate-name", "#nav-profile",
                                         "a[href*='logout']", "[class*='user-avatar']"])
LOGGED_OUT = T("instahyre logged out", css=["form#login-form", "input#email[type='email']", "a[href='/login/']"],
               text=["Login", "Log in"])

CARD_SELECTORS = ["div.employer-block", "div.opportunity-card", "[ng-repeat*='opportunit']", "div.job-card",
                  "li.opportunity"]
CARD_FIELDS = {
    "title": [".employer-job-name", ".job-title", ".opportunity-title", "h3", "h4"],
    "company": [".company-name", ".employer-name", "h2", ".company"],
    "location": [".employer-locations", ".job-locations", ".location", "[class*='location']"],
    "experience": [".employer-experience", ".experience", "[class*='experience']"],
    "snippet": [".job-description-snippet", ".employer-description", "p"],
}
CARD_MULTI = {"skills": [".skill", ".job-skill", ".tag", "[class*='skill'] li"]}
CARD_ATTRS = {"href": (["a[href*='/job-']", "a.job-link", "a[href*='job']"], "href"),
              "job_id": ([":self", "[data-job-id]"], "data-job-id")}
CARD_FLAGS = {"applied": [".applied", "[class*='applied']", "button[disabled].apply-btn"]}
LOAD_MORE = T("load more", css=["button.load-more", "#load-more"], text=["Load more", "Show more", "View more"])

DETAIL_TITLE = ["h1.job-title", ".job-title h1", "h1"]
DETAIL_COMPANY = [".company-name", "h2.company", ".employer-name", "h2"]
DETAIL_LOCATION = [".job-locations", ".employer-locations", ".location"]
DETAIL_EXP = [".job-experience", ".experience", "[class*='experience']"]
DETAIL_JD = [".job-description", "#job-description", "[class*='job-description']", ".description", "article"]
DETAIL_SKILLS = [".job-skills .skill", ".skills .skill", ".skill", ".tag"]

APPLY_BTN = T("instahyre apply", css=["button#interested-btn", "button.apply-btn:not([disabled])",
                                      "button[class*='apply']:not([disabled])"],
              role=("button", r"^\s*(apply|interested|i'?m\s+interested|apply\s+now)\s*$"),
              text=["Apply", "Interested", "I'm interested", "Apply now"])
APPLIED_STATE = T("instahyre applied", css=["button.applied", "button[disabled].apply-btn", ".already-applied",
                                            "[class*='applied-status']"],
                  role=("button", r"^\W*(?:applied|application\s+sent!?)\s*$"),  # 2026: "✓ Application sent!" + date
                  text=["Application sent!", "Application sent"])
MODAL = T("instahyre modal", css=[".modal.in", ".modal.show", "[role='dialog']:visible", ".apply-modal"])
MODAL_CONFIRM = T("instahyre confirm", css=["button#apply-confirm", ".modal button[type='submit']"],
                  role=("button", r"^\s*(apply|submit|send|confirm|apply now)\s*$"))
SUCCESS = T("instahyre success", css=[".apply-success", ".toast-success", "[class*='success-message']"],
            within_text=["successfully applied", "Application sent"])


_YEARS_LINE = re.compile(r"^\s*\d{1,2}(?:\.\d)?\s*(?:-|–|to)\s*\d{1,2}(?:\.\d)?\s*(?:years?|yrs?)\s*$|^\s*\d{1,2}\s*\+\s*(?:years?|yrs?)\s*$",
                         re.I)


def split_company_title(title: str, company: str = "") -> tuple[str, str]:
    """(company, role) from Instahyre's heading "Fractal Analytics - Senior Full-Stack Engineer". Both of its
    selectors return that whole heading, so the employer's name was part of every title: "Fractal Analytics - ..."
    was skipped as a data role, and no Instahyre job could be recognised as the same role on another portal."""
    title, company = (title or "").strip(), (company or "").strip()
    if " - " not in title or (company and company != title and not title.lower().startswith(company.lower() + " - ")):
        return company, title
    first, _, role = title.partition(" - ")
    return (first.strip(), role.strip()) if first.strip() and role.strip() else (company, title)


def split_location(text: str) -> tuple[str, str]:
    """('Chennai, Mumbai', '3-7 Years') from Instahyre's location block, which sometimes carries the experience
    range on its second line ("Chennai, Mumbai\n3-7 Years" was stored - and shown - as the location)."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    years = next((ln for ln in lines if _YEARS_LINE.match(ln)), "")
    return ", ".join(ln for ln in lines if not _YEARS_LINE.match(ln)), years


class InstahyreAdapter(PortalAdapter):
    name = "instahyre"
    display_name = "Instahyre"

    def __init__(self, engine, base_url: str) -> None:
        super().__init__(engine, base_url)
        self._opportunities_done = False

    @property
    def login_url(self) -> str:
        return f"{self.base_url}/login/"

    async def is_logged_in(self, navigate: bool = True) -> bool:
        page = self.engine.page
        if navigate:
            await self.engine.goto(f"{self.base_url}/candidate/opportunities/?matching=true")
        if "/login" in page.url.lower():
            return False
        key, _ = await self.engine.wait_for_any({"in": LOGGED_IN, "out": LOGGED_OUT},
                                                timeout_ms=8000 if navigate else 500)
        return key == "in"

    def _check_session(self) -> None:
        if "/login" in self.engine.page.url.lower():
            raise LoginRequired(self.name, "Instahyre session expired")

    # ------------------------------------------------------------ search
    def search_url(self, q: SearchQuery) -> str:
        params: dict[str, str | int] = {"skills": q.keywords}
        if q.location:
            params["locations"] = q.location
        if q.experience_years is not None:
            params["years"] = int(q.experience_years)
        return f"{self.base_url}/search-jobs/?{urlencode(params)}"

    async def search(self, query: SearchQuery) -> AsyncIterator[JobPosting]:  # type: ignore[override]
        urls = []
        if not self._opportunities_done:
            urls.append(f"{self.base_url}/candidate/opportunities/?matching=true")  # Instahyre's matched jobs
            self._opportunities_done = True
        urls.append(self.search_url(query))
        seen: set[str] = set()
        yielded = 0
        for url in urls:
            await self.engine.goto(url)
            self._check_session()
            key, _ = await self.engine.wait_for_any({"cards": T(css=CARD_SELECTORS)}, timeout_ms=12_000)
            if key is None:
                continue
            for _ in range(max(0, query.max_pages - 1)):
                more = await self.engine.find(LOAD_MORE, timeout_ms=800)
                if more is None:
                    break
                await self.engine.click(more)
                await self.engine.page.wait_for_timeout(800)
            await self.engine.scroll_to_load_all(T(css=CARD_SELECTORS), max_rounds=4)
            cards = await extract_cards(self.engine.page, CARD_SELECTORS, CARD_FIELDS, CARD_MULTI, CARD_ATTRS, CARD_FLAGS)
            for c in cards:
                job = self._card_to_job(c)
                if job is None or job.external_id in seen:
                    continue
                seen.add(job.external_id)
                yielded += 1
                yield job
                if yielded >= query.max_jobs:
                    return

    def _card_to_job(self, c: dict) -> JobPosting | None:
        href = c.get("href") or ""
        url = urljoin(self.base_url + "/", href) if href else ""
        job_id = c.get("job_id") or _id_from_url(url)
        if not job_id or not c.get("title"):
            return None
        location, years = split_location(c.get("location", ""))
        company, title = split_company_title(c["title"], c.get("company", ""))
        return JobPosting(portal=self.name, external_id=str(job_id), url=url or f"{self.base_url}/job-{job_id}-job/",
                          title=title, company=company, location=location,
                          experience_text=c.get("experience", "") or years, snippet=c.get("snippet", ""),
                          skills=c.get("skills") or [], already_applied=bool(c.get("applied")))

    # ----------------------------------------------------------- details
    async def fetch_details(self, job: JobPosting) -> JobPosting:
        await self.engine.goto(job.url)
        self._check_session()
        page = self.engine.page
        await self.engine.wait_for_any({"jd": T(css=DETAIL_JD[:3]), "title": T(css=DETAIL_TITLE[:2])}, timeout_ms=12_000)
        heading = await first_text(page, DETAIL_TITLE)
        named = (await first_text(page, DETAIL_COMPANY)).split("\n")[0]
        if heading:
            company, title = split_company_title(heading, named or job.company)
            job.title, job.company = title or job.title, company or job.company
        elif named and named != job.title:
            job.company = named
        location, years = split_location(await first_text(page, DETAIL_LOCATION))
        job.location = location or job.location
        job.experience_text = await first_text(page, DETAIL_EXP) or years or job.experience_text
        job.description = await first_text(page, DETAIL_JD) or job.description
        await read_page_dates(page, job)  # Instahyre lists no age; its job pages often carry datePosted
        skills = await all_texts(page, DETAIL_SKILLS)
        if skills:
            job.skills = list(dict.fromkeys([*job.skills, *skills]))
        key, _ = await self.engine.wait_for_any({"applied": APPLIED_STATE, "apply": APPLY_BTN}, timeout_ms=4000)
        job.already_applied = job.already_applied or key == "applied"
        job.apply_type = "portal" if key == "apply" else job.apply_type
        job.details_loaded = True
        return job

    # ------------------------------------------------------------- apply
    async def apply(self, job: JobPosting, ctx: ApplyContext) -> ApplyResult:
        page = self.engine.page
        if job.url.split("?")[0] not in page.url:
            await self.engine.goto(job.url)
            self._check_session()
        key, btn = await self.engine.wait_for_any({"applied": APPLIED_STATE, "apply": APPLY_BTN}, timeout_ms=10_000)
        if key == "applied":
            return ApplyResult(ApplyOutcome.ALREADY_APPLIED, "Instahyre shows this job as applied")
        if btn is None:
            found = await generic_apply_button(page)  # markup changed: find the button by what it says
            btn = found[1] if found else None
        if btn is None:
            if EXPIRED_TEXT.search(await self.engine.body_text(page, limit=3000)):
                return ApplyResult(ApplyOutcome.EXPIRED, "Job is no longer accepting applications")
            return ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED, "Apply button not found",
                               screenshot=await self.engine.screenshot(f"instahyre-noapply-{job.external_id}"))
        decision = await ctx.decide_submit(
            page, unresolved=[], summary=f"Apply to '{job.title}' at {job.company} on Instahyre",
            detect_submitted=lambda: self._success(page))
        if decision == SubmitDecision.DRY_RUN:
            return ApplyResult(ApplyOutcome.DRY_RUN, "Dry run: would click Apply on Instahyre")
        if decision == SubmitDecision.REJECTED:
            return ApplyResult(ApplyOutcome.REJECTED, "You rejected this application")
        if decision == SubmitDecision.MANUAL:
            return ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED, "Not submitted - needs your review")
        if decision == SubmitDecision.SUBMITTED_BY_USER:
            text = await self._success(page)
            return ApplyResult(ApplyOutcome.APPLIED if text else ApplyOutcome.MANUAL_ACTION_REQUIRED,
                               "Submitted by you", confirmation_text=text, submit_attempted=True)
        self._before = await page_state(page)  # text already on the page never confirms
        await ctx.mark_submit_attempt()
        await self.engine.click(btn)
        for _ in range(3):
            key, _el = await self.engine.wait_for_any({"success": SUCCESS, "modal": MODAL, "applied": APPLIED_STATE},
                                                      timeout_ms=8000)
            text = await self._success(page)
            if not text and key == "applied":  # the Apply button turned into Instahyre's 'Applied' state
                text = "Instahyre now shows this job as applied"
            if text:
                return ApplyResult(ApplyOutcome.APPLIED, "Applied on Instahyre", confirmation_text=text,
                                   submit_attempted=True, verification="confirmed",
                                   evidence=[f"portal_confirmation: {text[:160]}"])
            if key == "modal":
                res = await self._complete_modal(page, ctx)
                if res is not None:
                    return res
                continue
            break
        return ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED,
                           "Submitted - confirmation not verified: clicked Apply, Instahyre showed neither its success "
                           "message nor the 'Applied' state", submit_attempted=True, verification="uncertain",
                           evidence=["apply_clicked"], screenshot=await self.engine.screenshot("instahyre-unconfirmed"))

    async def _complete_modal(self, page: Page, ctx: ApplyContext) -> ApplyResult | None:
        modal = await self.engine.find(MODAL, timeout_ms=2000)
        if modal is None:
            return None
        filler = ctx.form_filler()
        report = await filler.fill(modal)
        unresolved = summarize_unresolved(report)
        if unresolved:
            # the same question flow as every other portal: asked once in the dashboard, filled in, remembered
            out = await ask_and_fill(ctx, page, modal, filler, report.unresolved)
            if await self._success(page):  # you finished it in the browser meanwhile
                return ApplyResult(ApplyOutcome.APPLIED, "Applied on Instahyre (completed by you)",
                                   confirmation_text=await self._success(page), submit_attempted=True)
            if out.skipped_by_you:
                return ApplyResult(ApplyOutcome.REJECTED, "You chose to skip this application", submit_attempted=True)
            if not out.answered or out.still_missing:
                missing = out.still_missing or unresolved
                return ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED,
                                   "Instahyre questions need your answers: " + "; ".join(missing[:4]),
                                   submit_attempted=True, unresolved=missing)
        confirm = await self.engine.find(MODAL_CONFIRM, root=modal, timeout_ms=2000)
        if confirm is None:
            return ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED, "Could not find the confirm button",
                               submit_attempted=True)
        await self.engine.click(confirm)
        await page.wait_for_timeout(500)
        errors = await collect_errors(modal) if await modal.is_visible() else []
        if errors:
            return ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED, "Instahyre form errors: " + "; ".join(errors[:3]),
                               submit_attempted=True)
        return None

    async def _success(self, page: Page) -> str:
        try:
            if await self.engine.exists(SUCCESS, root=page):
                return await self.engine.text_of(SUCCESS, root=page) or "Applied"
        except PlaywrightError:
            return ""
        before = getattr(self, "_before", None)
        return await confirmation_since(page, before) if before is not None else ""


def _id_from_url(url: str) -> str:
    m = re.search(r"/job-(\d+)", url or "")
    return m.group(1) if m else ""
