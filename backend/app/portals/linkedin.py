"""LinkedIn adapter: job search, details, Easy Apply (multi-step) and external redirects.

All LinkedIn selectors live in this module. External "Apply" buttons hand over to the ATS layer.
Easy Apply drafts are discarded whenever we stop before submitting (dry run, rejection, manual).
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlencode

from playwright.async_api import Error as PlaywrightError, Locator, Page

from app.browser.errors import LoginRequired
from app.browser.forms import FormFiller, collect_errors, summarize_unresolved
from app.browser.selectors import T
from app.engine.context import ApplyContext, GateResult, SubmitDecision
from app.portals.base import PortalAdapter, SearchQuery
from app.portals.common import extract_cards, first_text, page_has_success
from app.portals.types import ApplyOutcome, ApplyResult, JobPosting

log = logging.getLogger(__name__)

LOGGED_IN = T("linkedin logged in", css=[".global-nav__me", "img.global-nav__me-photo", "#global-nav .global-nav__me",
                                        "[data-control-name='nav.settings']", "nav.global-nav"])
LOGGED_OUT = T("linkedin logged out", css=["form.login__form", "#username", "a.nav__button-secondary",
                                           "[data-tracking-control-name='guest_homepage-basic_nav-header-signin']"])

CARD_SELECTORS = ["li[data-occludable-job-id]", "div.job-card-container[data-job-id]", "li.jobs-search-results__list-item",
                  "li.scaffold-layout__list-item", "div[data-job-id]"]
CARD_FIELDS = {
    "title": ["a.job-card-list__title--link strong", "a.job-card-list__title", ".job-card-list__title--link",
              ".artdeco-entity-lockup__title", "a.job-card-container__link"],
    "company": [".artdeco-entity-lockup__subtitle", ".job-card-container__primary-description",
                ".job-card-container__company-name"],
    "location": [".job-card-container__metadata-wrapper li", ".artdeco-entity-lockup__caption li",
                 ".job-card-container__metadata-item", ".artdeco-entity-lockup__caption"],
    "footer": [".job-card-container__footer-wrapper", "ul.job-card-list__footer-wrapper", ".job-card-container__footer-item"],
}
CARD_ATTRS = {
    "occ_id": ([":self"], "data-occludable-job-id"),
    "job_id": ([":self", "[data-job-id]"], "data-job-id"),
    "href": (["a.job-card-list__title--link", "a.job-card-list__title", "a.job-card-container__link", "a[href*='/jobs/view/']"], "href"),
}
LIST_CONTAINER = ".jobs-search-results-list, .scaffold-layout__list > div, .scaffold-layout__list"

DETAIL_TITLE = [".job-details-jobs-unified-top-card__job-title h1", ".job-details-jobs-unified-top-card__job-title",
                "h1.t-24", ".top-card-layout__title", "h1"]
DETAIL_COMPANY = [".job-details-jobs-unified-top-card__company-name a", ".job-details-jobs-unified-top-card__company-name",
                  ".topcard__org-name-link", ".jobs-unified-top-card__company-name"]
DETAIL_LOCATION = [".job-details-jobs-unified-top-card__primary-description-container .tvm__text",
                   ".job-details-jobs-unified-top-card__bullet", ".topcard__flavor--bullet",
                   ".job-details-jobs-unified-top-card__tertiary-description-container span"]
DETAIL_JD = ["#job-details", ".jobs-description__content", ".jobs-description-content__text",
             ".show-more-less-html__markup", ".description__text"]

APPLY_BTN = T("linkedin apply", css=["button.jobs-apply-button", ".jobs-apply-button--top-card button",
                                     "button[aria-label*='Easy Apply']", "button[aria-label*='Apply to']"],
              role=("button", r"^\s*(easy\s+)?apply\b"))
APPLIED_STATE = T("linkedin applied", css=[".artdeco-inline-feedback--success", ".post-apply-timeline__entity",
                                           ".jobs-s-apply__application-link"],
                  within_text=["/^\\s*Applied\\s+\\d+\\s+(?:second|minute|hour|day|week|month)s?\\s+ago/i",
                               "/^\\s*Application submitted\\s*$/i"])
NO_LONGER = T("closed", within_text=["No longer accepting applications"])

MODAL = T("easy apply modal", css=[".jobs-easy-apply-modal", "div[data-test-modal][role='dialog']",
                                   "div.artdeco-modal[role='dialog']", "[role='dialog']"])
NEXT_BTN = T("next", css=["button[aria-label='Continue to next step']", "button[data-easy-apply-next-button]",
                          "button[data-live-test-easy-apply-next-button]"], role=("button", r"^\s*next\s*$"))
REVIEW_BTN = T("review", css=["button[aria-label='Review your application']", "button[data-live-test-easy-apply-review-button]"],
               role=("button", r"^\s*review\s*$"))
SUBMIT_BTN = T("submit", css=["button[aria-label='Submit application']", "button[data-live-test-easy-apply-submit-button]"],
               role=("button", r"^\s*submit\s+application\s*$"))
DISMISS = T("dismiss", css=["button[aria-label='Dismiss']", "button.artdeco-modal__dismiss"])
DISCARD = T("discard", css=["button[data-control-name='discard_application_confirm_btn']",
                            "button[data-test-dialog-secondary-btn]"], text=["Discard"])
POST_APPLY = T("post apply", css=["[data-test-modal-id='post-apply-modal']", ".jobs-post-apply-modal",
                                  "#post-apply-modal"],
               within_text=["Your application was sent", "Application sent"])
FOLLOW_COMPANY = "input#follow-company-checkbox, input[id*='follow-company']"
RESUME_CARDS = ".jobs-document-upload-redesign-card__container, .jobs-resume-picker__resume"
CONTINUE_EXTERNAL = T("continue to company site", css=["[role='dialog'] button.artdeco-button--primary"],
                      text=["Continue", "Continue applying"])


class LinkedInAdapter(PortalAdapter):
    name = "linkedin"
    display_name = "LinkedIn"

    @property
    def login_url(self) -> str:
        return f"{self.base_url}/login"

    async def is_logged_in(self, navigate: bool = True) -> bool:
        page = self.engine.page
        if navigate:
            await self.engine.goto(f"{self.base_url}/feed/")
        url = page.url.lower()
        if any(x in url for x in ("/login", "/authwall", "/checkpoint", "uas/login", "/signup")):
            return False
        key, _ = await self.engine.wait_for_any({"in": LOGGED_IN, "out": LOGGED_OUT},
                                                timeout_ms=8000 if navigate else 500)
        return key == "in"

    def _check_session(self) -> None:
        url = self.engine.page.url.lower()
        if "/checkpoint" in url:
            raise LoginRequired(self.name, "LinkedIn security checkpoint (CAPTCHA/verification) - please complete it")
        if any(x in url for x in ("/login", "/authwall", "uas/login")):
            raise LoginRequired(self.name, "LinkedIn session expired")

    # ------------------------------------------------------------ search
    def search_url(self, q: SearchQuery, page_no: int) -> str:
        params: dict[str, str | int] = {"keywords": q.keywords, "sortBy": "DD"}
        if q.location:
            params["location"] = q.location
        if q.posted_within_days:
            params["f_TPR"] = f"r{int(q.posted_within_days) * 86400}"
        if q.easy_apply_only:
            params["f_AL"] = "true"
        if page_no > 1:
            params["start"] = 25 * (page_no - 1)
        return f"{self.base_url}/jobs/search/?{urlencode(params)}"

    async def search(self, query: SearchQuery) -> AsyncIterator[JobPosting]:  # type: ignore[override]
        seen: set[str] = set()
        yielded = 0
        for page_no in range(1, max(1, query.max_pages) + 1):
            await self.engine.goto(self.search_url(query, page_no))
            self._check_session()
            key, _ = await self.engine.wait_for_any(
                {"cards": T(css=CARD_SELECTORS), "none": T(css=[".jobs-search-no-results-banner",
                                                                ".jobs-search-two-pane__no-results-banner--expand"],
                                                           within_text=["No matching jobs found"])}, timeout_ms=15_000)
            if key != "cards":
                break
            container = await self.engine.page.evaluate(
                "(sels) => { for (const s of sels.split(',')) { if (document.querySelector(s.trim())) return s.trim(); } return null; }",
                LIST_CONTAINER)
            await self.engine.scroll_to_load_all(T(css=CARD_SELECTORS), max_rounds=6, container_selector=container)
            cards = await extract_cards(self.engine.page, CARD_SELECTORS, CARD_FIELDS, attrs=CARD_ATTRS)
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
        job_id = c.get("occ_id") or c.get("job_id") or _id_from_url(c.get("href") or "")
        title = (c.get("title") or "").split("\n")[0].strip()
        if not job_id or not title:
            return None
        footer = (c.get("footer") or "") + " " + (c.get("_text") or "")
        return JobPosting(portal=self.name, external_id=str(job_id), url=f"{self.base_url}/jobs/view/{job_id}/",
                          title=re.sub(r"\s+with verification$", "", title), company=c.get("company", ""),
                          location=c.get("location", ""), easy_apply="Easy Apply" in footer,
                          already_applied=bool(re.search(r"\bApplied\b", c.get("footer") or "")))

    # ----------------------------------------------------------- details
    async def fetch_details(self, job: JobPosting) -> JobPosting:
        await self.engine.goto(job.url)
        self._check_session()
        page = self.engine.page
        await self.engine.wait_for_any({"jd": T(css=DETAIL_JD), "title": T(css=DETAIL_TITLE[:3])}, timeout_ms=15_000)
        more = await self.engine.find(T(css=["button.jobs-description__footer-button",
                                             "button[aria-label*='see more description' i]"]), timeout_ms=500)
        if more is not None:
            try:
                await more.click()
            except PlaywrightError:
                pass
        job.title = (await first_text(page, DETAIL_TITLE)).split("\n")[0] or job.title
        job.company = (await first_text(page, DETAIL_COMPANY)).split("\n")[0] or job.company
        loc = await first_text(page, DETAIL_LOCATION)
        if loc:
            job.location = loc.split("·")[0].strip()
        job.description = await first_text(page, DETAIL_JD) or job.description
        key, btn = await self.engine.wait_for_any({"applied": APPLIED_STATE, "apply": APPLY_BTN, "closed": NO_LONGER},
                                                  timeout_ms=5000)
        if key == "applied":
            job.already_applied = True
        elif key == "closed":
            job.extra["expired"] = True
        elif key == "apply" and btn is not None:
            label = ((await btn.get_attribute("aria-label")) or "") + " " + (await btn.inner_text())
            job.easy_apply = "easy apply" in label.lower()
            job.apply_type = "portal" if job.easy_apply else "external"
        job.details_loaded = True
        return job

    # ------------------------------------------------------------- apply
    async def apply(self, job: JobPosting, ctx: ApplyContext) -> ApplyResult:
        page = self.engine.page
        if f"/jobs/view/{job.external_id}" not in page.url:
            await self.engine.goto(job.url)
            self._check_session()
        key, btn = await self.engine.wait_for_any({"applied": APPLIED_STATE, "apply": APPLY_BTN, "closed": NO_LONGER},
                                                  timeout_ms=10_000)
        if key == "applied":
            return ApplyResult(ApplyOutcome.ALREADY_APPLIED, "LinkedIn shows this job as applied")
        if key == "closed":
            return ApplyResult(ApplyOutcome.FAILED, "No longer accepting applications")
        if btn is None:
            return ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED, "Apply button not found",
                               screenshot=await self.engine.screenshot(f"linkedin-noapply-{job.external_id}"))
        label = ((await btn.get_attribute("aria-label")) or "") + " " + (await btn.inner_text())
        if "easy apply" not in label.lower():
            return await self._external(page, btn, ctx)
        await self.engine.click(btn)
        modal = await self.engine.find(MODAL, timeout_ms=10_000)
        if modal is None:
            return ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED, "Easy Apply dialog did not open",
                               screenshot=await self.engine.screenshot(f"linkedin-nomodal-{job.external_id}"))
        return await self._easy_apply(page, ctx)

    async def _external(self, page: Page, btn: Locator, ctx: ApplyContext) -> ApplyResult:
        await ctx.event("external", "LinkedIn job applies on the company website")
        new_page = await self.engine.expect_popup(lambda: self.engine.click(btn), timeout_ms=8000)
        if new_page is None:
            cont = await self.engine.find(CONTINUE_EXTERNAL, timeout_ms=2000)
            if cont is not None:
                new_page = await self.engine.expect_popup(lambda: self.engine.click(cont), timeout_ms=8000)
        if new_page is None:
            return ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED, "Company website did not open")
        return ApplyResult(ApplyOutcome.EXTERNAL, "External application", external_url=new_page.url,
                           external_page=new_page)

    async def _modal(self) -> Locator | None:
        return await self.engine.find(MODAL, timeout_ms=1500)

    async def _easy_apply(self, page: Page, ctx: ApplyContext) -> ApplyResult:
        filler = ctx.form_filler()
        all_unresolved: list[str] = []
        for step in range(15):
            await self.engine.checkpoint()
            if await self._success(page):
                return await self._applied(page)
            modal = await self._modal()
            if modal is None:
                return ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED, "Easy Apply dialog closed unexpectedly")
            await self._select_resume(modal, ctx, filler)
            report = await filler.fill(modal)
            unresolved = summarize_unresolved(report)
            all_unresolved.extend(u for u in unresolved if u not in all_unresolved)
            submit = await self.engine.find(SUBMIT_BTN, root=modal)
            if submit is not None:
                return await self._submit(page, modal, submit, ctx, all_unresolved)
            nxt = await self.engine.find(REVIEW_BTN, root=modal) or await self.engine.find(NEXT_BTN, root=modal)
            if nxt is None:
                await self._discard(page)
                return ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED, "Unknown Easy Apply step",
                                   unresolved=all_unresolved)
            if unresolved:
                res = await self._need_human(page, ctx, unresolved, all_unresolved)
                if res is not None:
                    return res
            header = await self._step_marker(modal)
            await self.engine.click(nxt)
            await page.wait_for_timeout(400)
            for _ in range(20):
                if await self._step_marker(modal) != header or await self._success(page):
                    break
                await page.wait_for_timeout(250)
            else:
                errors = await collect_errors(modal)
                if errors:
                    res = await self._need_human(page, ctx, errors, all_unresolved)
                    if res is not None:
                        return res
        await self._discard(page)
        return ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED, "Too many Easy Apply steps", unresolved=all_unresolved)

    async def _need_human(self, page: Page, ctx: ApplyContext, problems: list[str],
                          all_unresolved: list[str]) -> ApplyResult | None:
        if ctx.mode.value == "dry_run":
            await ctx.event("dry_run", "Dry run stopped at a step with questions needing review", unresolved=problems)
            await self._discard(page)
            return ApplyResult(ApplyOutcome.DRY_RUN, "Dry run: questions need review", unresolved=all_unresolved)
        res = await ctx.wait_for_human("manual", "Please answer in the LinkedIn dialog, then press Continue: "
                                       + "; ".join(problems[:5]), page=page)
        if res in (GateResult.RESOLVED, GateResult.APPROVE):
            return None
        await self._discard(page)
        return ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED,
                           "Questions need your answers: " + "; ".join(problems[:5]), unresolved=all_unresolved)

    async def _submit(self, page: Page, modal: Locator, submit: Locator, ctx: ApplyContext,
                      unresolved: list[str]) -> ApplyResult:
        try:
            follow = modal.locator(FOLLOW_COMPANY)
            if await follow.count() and await follow.first.is_checked():
                await follow.first.evaluate("el => (el.closest('label') || document.querySelector('label[for=\"' + el.id + '\"]') || el).click()")
        except PlaywrightError:
            pass
        decision = await ctx.decide_submit(page, unresolved=unresolved,
                                           summary=f"LinkedIn Easy Apply for '{ctx.job.title}' at {ctx.job.company} is ready",
                                           detect_submitted=lambda: self._success(page))
        if decision == SubmitDecision.DRY_RUN:
            await self._discard(page)
            return ApplyResult(ApplyOutcome.DRY_RUN, "Dry run: Easy Apply filled, discarded before submit",
                               unresolved=unresolved)
        if decision == SubmitDecision.REJECTED:
            await self._discard(page)
            return ApplyResult(ApplyOutcome.REJECTED, "You rejected this application")
        if decision == SubmitDecision.MANUAL:
            await self._discard(page)
            return ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED,
                               "Not submitted: " + ("; ".join(unresolved[:5]) or "needs your review"),
                               unresolved=unresolved)
        if decision == SubmitDecision.SUBMITTED_BY_USER:
            return await self._applied(page) if await self._success(page) else ApplyResult(
                ApplyOutcome.MANUAL_ACTION_REQUIRED, "Submitted by you, confirmation not detected", submit_attempted=True)
        await ctx.mark_submit_attempt()
        await self.engine.click(submit)
        for _ in range(40):
            if await self._success(page):
                return await self._applied(page)
            errors = await collect_errors(modal) if await modal.count() else []
            if errors:
                return ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED, "LinkedIn rejected the submission: "
                                   + "; ".join(errors[:3]), submit_attempted=True)
            await page.wait_for_timeout(500)
        return ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED, "Submitted but no confirmation detected",
                           submit_attempted=True, screenshot=await self.engine.screenshot("linkedin-unconfirmed"))

    async def _applied(self, page: Page) -> ApplyResult:
        text = await self.engine.text_of(POST_APPLY) or await page_has_success(page) or "Application sent"
        dismiss = await self.engine.find(DISMISS, timeout_ms=500)
        if dismiss is not None:
            try:
                await dismiss.click()
            except PlaywrightError:
                pass
        return ApplyResult(ApplyOutcome.APPLIED, "Applied with LinkedIn Easy Apply",
                           confirmation_text=text.split("\n")[0][:300], submit_attempted=True)

    async def _success(self, page: Page) -> bool:
        if await self.engine.exists(POST_APPLY, root=page):
            return True
        return bool(await self.engine.exists(T(css=["[role='dialog']:has-text('Your application was sent')",
                                                     "[role='dialog']:has-text('Application sent')"]), root=page))

    async def _step_marker(self, modal: Locator) -> str:
        try:
            return await modal.evaluate("el => { const h = el.querySelector('h3, h2, .t-16'); const p = el.querySelector('progress, [role=progressbar]');"
                                        " return (h ? h.innerText : '') + '|' + (p ? (p.value || p.getAttribute('aria-valuenow')) : '') + '|' + el.querySelectorAll('input,select,textarea').length; }")
        except PlaywrightError:
            return ""

    async def _select_resume(self, modal: Locator, ctx: ApplyContext, filler: FormFiller) -> None:
        """Prefer an already-uploaded resume with the same file name instead of re-uploading."""
        if ctx.resume_path is None:
            return
        cards = modal.locator(RESUME_CARDS)
        try:
            n = await cards.count()
        except PlaywrightError:
            return
        if not n:
            return
        wanted = Path(ctx.resume_path).name.lower()
        for i in range(n):
            card = cards.nth(i)
            try:
                if not await card.is_visible():
                    continue
                text = (await card.inner_text()).lower()
                if wanted not in text:
                    continue
                cls = (await card.get_attribute("class")) or ""
                if "selected" not in cls and (await card.get_attribute("aria-label") or "").lower() != "selected":
                    await card.click()
            except PlaywrightError as exc:
                log.debug("resume card: %s", exc)
                continue
            filler.mark_resume_handled()
            await ctx.event("resume", f"Selected existing LinkedIn resume {wanted}")
            return

    async def _discard(self, page: Page) -> None:
        dismiss = await self.engine.find(DISMISS, timeout_ms=1000)
        if dismiss is None:
            return
        try:
            await dismiss.click()
            discard = await self.engine.find(DISCARD, timeout_ms=3000)
            if discard is not None:
                await discard.click()
        except PlaywrightError as exc:
            log.debug("discard failed: %s", exc)


def _id_from_url(url: str) -> str:
    m = re.search(r"/jobs/view/(\d+)", url or "") or re.search(r"currentJobId=(\d+)", url or "")
    return m.group(1) if m else ""
