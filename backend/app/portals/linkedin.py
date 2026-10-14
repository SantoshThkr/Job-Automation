"""LinkedIn adapter: job search, details, Easy Apply (multi-step) and external redirects.

All LinkedIn selectors live in this module. External "Apply" buttons hand over to the ATS layer.
Easy Apply drafts are discarded whenever we stop before submitting (dry run, rejection, manual).
"""

from __future__ import annotations

import asyncio
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
from app.engine.questions import ask_and_fill
from app.engine.submission import SubmissionState, SubmissionWatcher
from app.portals.base import PortalAdapter, SearchQuery
from app.portals.common import extract_cards, first_text, read_page_dates
from app.portals.linkedin_session import LinkedInSession, LinkedInState, LoginCheck, classify_page
from app.portals.types import ApplyOutcome, ApplyResult, JobPosting

log = logging.getLogger(__name__)

# Sign-in state is decided from several signals in portals/linkedin_session.py (cookie, URL, page, all tabs).

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
# 2026 job page: generated class names, no <h1>; stable hooks are data-view-name attributes and the headings.
DETAIL_PAGE = "[data-view-name='job-detail-page']"
DETAIL_JS = r"""(root) => {
  const T = e => (e && e.innerText || '').trim();
  const scope = document.querySelector(root) || document.querySelector('main') || document.body;
  const heads = Array.from(scope.querySelectorAll('h1, h2, h3, h4, span, p, div'))
    .filter(e => /^about\s+the\s+job$/i.test(T(e)) && e.children.length <= 2);
  let jd = '';
  if (heads.length) {
    let box = heads[0];
    while (box.parentElement && box !== scope && T(box).length < T(heads[0]).length + 200) box = box.parentElement;
    jd = T(box).replace(/^\s*about\s+the\s+job\s*/i, '');
  }
  // "(3) Title | Co | LinkedIn" - the company is the part before "LinkedIn": a title may contain " | " itself
  // ("Software Engineer (Remote | $50-$100/hr)"), and reading the second part made the pay its company
  const parts = document.title.replace(/^\(\d+\+?\)\s*/, '').split(' | ').map(x => x.trim());
  const n = parts.length;
  return {jd: jd.slice(0, 20000), title: n >= 3 ? parts.slice(0, n - 2).join(' | ') : '', company: n >= 3 ? parts[n - 2] : ''};
}"""

APPLY_BTN = T("linkedin apply", css=["button.jobs-apply-button", ".jobs-apply-button--top-card button",
                                     "[data-view-name='job-apply-button']",
                                     "button[aria-label*='Easy Apply']", "button[aria-label*='Apply to']",
                                     # 2026: the control is often a link, sometimes without the data-view-name hook
                                     "a[aria-label^='Easy Apply' i]", "a[aria-label*='Apply on company' i]",
                                     "a[aria-label^='Apply to' i]"],
              role=[("button", r"^\s*(easy\s+)?apply\b"), ("link", r"^\s*easy\s+apply\b")])
APPLIED_STATE = T("linkedin applied", css=[".artdeco-inline-feedback--success", ".post-apply-timeline__entity",
                                           ".jobs-s-apply__application-link"],
                  within_text=["/^\\s*Applied\\s+\\d+\\s+(?:second|minute|hour|day|week|month)s?\\s+ago/i",
                               "/^\\s*Application submitted\\s*$/i"])
APPLIED_TEXT = T("linkedin applied text", within_text=list(APPLIED_STATE.within_text))  # job page after submitting
# (LinkedIn says "Not currently accepting applications" on a paused posting: no Apply button, nothing to do there)
NO_LONGER = T("closed", within_text=["No longer accepting applications", "Not currently accepting applications",
                                     "/^\\s*This job is no longer available/i"])

# 2026 Easy Apply: a native <dialog open data-testid="dialog" aria-labelledby="dialog-header"> (no role attribute)
MODAL = T("easy apply modal", css=[".jobs-easy-apply-modal", "div[data-test-modal][role='dialog']",
                                   "div.artdeco-modal[role='dialog']", "dialog[open][data-testid='dialog']",
                                   "dialog[open][aria-labelledby='dialog-header']", "dialog[open]", "[role='dialog']"])
NEXT_BTN = T("next", css=["button[aria-label='Continue to next step']", "button[data-easy-apply-next-button]",
                          "button[data-live-test-easy-apply-next-button]", "button[data-view-name='continue-unify']"],
             role=("button", r"^\s*next\s*$"))
REVIEW_BTN = T("review", css=["button[aria-label='Review your application']", "button[data-live-test-easy-apply-review-button]"],
               role=("button", r"^\s*review\s*$"))
SUBMIT_BTN = T("submit", css=["button[aria-label='Submit application']", "button[data-live-test-easy-apply-submit-button]"],
               role=("button", r"^\s*submit\s+application\s*$"))
DISMISS = T("dismiss", css=["button[aria-label='Dismiss']", "button.artdeco-modal__dismiss"], role=("button", r"^\s*dismiss\s*$"))
DISCARD = T("discard", css=["button[data-control-name='discard_application_confirm_btn']",
                            "button[data-test-dialog-secondary-btn]"], text=["Discard"])
POST_APPLY = T("post apply", css=["[data-test-modal-id='post-apply-modal']", ".jobs-post-apply-modal",
                                  "#post-apply-modal"])
# LinkedIn's own confirmation, read only inside dialogs / status messages (never from the job description text)
CONFIRMATION_JS = r"""() => {
  const rx = /your application was sent|application (?:was )?sent\b|application (?:was )?submitted/i;
  const boxes = document.querySelectorAll("dialog[open], [role=dialog], [role=alertdialog], [role=alert], [role=status], "
    + "[data-test-modal-id='post-apply-modal'], .jobs-post-apply-modal, #post-apply-modal, .artdeco-toast-item");
  for (const b of boxes) {
    if (!b.getClientRects().length) continue;
    const t = (b.innerText || '').replace(/\s+/g, ' ').trim();
    const m = t.match(rx);
    if (m) {
      const lines = (b.innerText || '').split('\n').map(x => x.trim()).filter(Boolean);
      return lines.find(x => /your application was sent/i.test(x)) || lines.find(x => rx.test(x)) || m[0];
    }
  }
  return '';
}"""
# The posting's age in the job's top card ("India · 15 hours ago · Over 100 applicants", "Reposted 2 days ago"): the
# first short line of the page that has the "·" separators and an "ago" (job lists further down have neither form)
_TOP_CARD_AGE_JS = r"""() => {
  const rx = /((?:Reposted\s+)?\d+\+?\s*(?:minute|hour|day|week|month|year)s?\s+ago)/i;
  for (const el of document.querySelectorAll('main p, main span, main div, .jobs-unified-top-card, .job-details-jobs-unified-top-card__primary-description-container')) {
    const t = (el.innerText || '').trim();
    if (!t || t.length > 160 || !t.includes('·')) continue;
    const m = t.match(rx);
    if (m) return m[1];
  }
  return '';
}"""
# LinkedIn ticks "Follow <company> to stay up to date with their page" on the review step. It is unticked - found by its
# id or, in the 2026 dialog where the box has no recognisable id, by its label (on 2026-10-14 the review step of a
# prepared application still had it ticked). Returns [found, still ticked].
_UNFOLLOW_JS = r"""(root) => {
  const label = (e) => ((e.labels && e.labels[0] && e.labels[0].innerText) || e.getAttribute('aria-label') || '').trim();
  const boxes = Array.from(root.querySelectorAll('input[type="checkbox"]')).filter(e =>
    /follow-company/i.test(e.id || '') || /^follow\b[\s\S]{1,120}\bstay up to date\b/i.test(label(e)));
  for (const e of boxes) {
    if (e.checked) ((e.labels && e.labels[0]) || e.closest('label') || e).click();
  }
  return [boxes.length, boxes.filter(e => e.checked).length];
}"""
RESUME_CARDS = ".jobs-document-upload-redesign-card__container, .jobs-resume-picker__resume"
CONTINUE_EXTERNAL = T("continue to company site", css=["[role='dialog'] button.artdeco-button--primary"],
                      text=["Continue", "Continue applying"])


_SAYS_SENT = re.compile(r"application\s+(?:was\s+)?(?:sent|submitted)|\bapplied\b", re.I)


def confirmation_line(text: str, seen: str = "") -> str:
    """The line to quote as LinkedIn's confirmation: the one that says the application was sent - from what is on the
    page now, else from what was shown when the submit was verified (``seen``). The post-apply panel starts with
    other headings ("Next best action"), which say nothing by themselves."""
    for source in (text, seen):
        lines = [ln.strip() for ln in (source or "").splitlines() if ln.strip()]
        said = next((ln for ln in lines if _SAYS_SENT.search(ln)), "")
        if said:
            return said
    lines = [ln.strip() for ln in (text or seen or "").splitlines() if ln.strip()]
    return lines[0] if lines else "LinkedIn"


class LinkedInAdapter(PortalAdapter):
    name = "linkedin"
    display_name = "LinkedIn"

    @property
    def login_url(self) -> str:
        return f"{self.base_url}/login"

    def session(self) -> LinkedInSession:
        """Sign-in state from all tabs + the profile's session cookie (see portals/linkedin_session.py)."""
        ctx = self.engine.context
        if getattr(self, "_session", None) is None or self._session.context is not ctx:
            self._session = LinkedInSession(ctx, self.base_url)
        return self._session

    async def login_state(self, navigate: bool = False, settle_s: float = 8) -> LoginCheck:
        """navigate=True opens /feed/ (members only) and judges that page; navigate=False inspects every open
        LinkedIn tab without touching them (used while you sign in), confirming in a background tab if needed."""
        if not navigate:
            return await self.session().check(confirm=True)
        page = await self.engine.goto(f"{self.base_url}/feed/")
        check = await self.session().check(confirm=False, pages=[page])
        deadline = asyncio.get_running_loop().time() + settle_s
        while check.state == LinkedInState.UNKNOWN and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.5)  # client-side redirects / React rendering
            check = await self.session().check(confirm=False, pages=[page])
        return check

    async def is_logged_in(self, navigate: bool = True) -> bool:
        return (await self.login_state(navigate)).authenticated

    def _check_session(self) -> None:
        verdict = classify_page(self.engine.page.url, None, session_cookie=True, remembered=False)
        if verdict.state == LinkedInState.VERIFICATION:
            raise LoginRequired(self.name, "LinkedIn security checkpoint (CAPTCHA/verification) - please complete it")
        if verdict.state in (LinkedInState.LOGIN_PAGE, LinkedInState.EXPIRED):
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
        key, _ = await self.engine.wait_for_any({"jd": T(css=DETAIL_JD), "title": T(css=DETAIL_TITLE[:3]),
                                                 "page": T(css=[DETAIL_PAGE]),
                                                 "about": T(within_text=["About the job"])}, timeout_ms=15_000)
        more = await self.engine.find(T(css=["button.jobs-description__footer-button",
                                             "button[aria-label*='see more description' i]"]), timeout_ms=500)
        if more is not None:
            try:
                await more.click()
            except PlaywrightError:
                pass
        if key in {"page", "about"} and not await self.engine.exists(T(css=DETAIL_JD)):
            await self._read_new_layout(job)
        else:
            job.title = (await first_text(page, DETAIL_TITLE)).split("\n")[0] or job.title
            job.company = (await first_text(page, DETAIL_COMPANY)).split("\n")[0] or job.company
            loc = await first_text(page, DETAIL_LOCATION)
            if loc:
                job.location = loc.split("·")[0].strip()
            job.description = await first_text(page, DETAIL_JD) or job.description
        if not job.description:  # any other layout: the 'About the job' section by its heading
            await self._read_new_layout(job)
        try:  # the top card's "United States · 3 hours ago · 88 applicants" (lower widgets say "Posted 1 week ago")
            job.posted_text = (await page.evaluate(_TOP_CARD_AGE_JS)) or job.posted_text
        except PlaywrightError:
            pass
        await read_page_dates(page, job)
        key, btn = await self.engine.wait_for_any({"applied": APPLIED_STATE, "apply": APPLY_BTN, "closed": NO_LONGER},
                                                  timeout_ms=5000)
        if key == "applied":
            job.already_applied = True
        elif key == "closed":
            job.extra["expired"] = True
            job.extra["expired_reason"] = ((await self.engine.text_of(NO_LONGER)) or "").strip().splitlines()[0][:160] \
                if btn is not None else ""
        elif key == "apply" and btn is not None:
            label = ((await btn.get_attribute("aria-label")) or "") + " " + (await btn.inner_text())
            job.easy_apply = "easy apply" in label.lower()
            job.apply_type = "portal" if job.easy_apply else "external"
        job.details_loaded = True
        return job

    async def _read_new_layout(self, job: JobPosting) -> None:
        """2026 job page: the description is the 'About the job' section; title/company come from the page title."""
        page = self.engine.page
        data: dict = {}
        for _ in range(10):  # the description section renders a moment after the top card
            try:
                data = await page.evaluate(DETAIL_JS, DETAIL_PAGE)
            except PlaywrightError as exc:
                log.debug("job page read failed: %s", exc)
            if data.get("jd"):
                break
            await page.wait_for_timeout(500)
        job.title = data.get("title") or job.title
        job.company = data.get("company") or job.company
        job.description = data.get("jd") or job.description

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
            said = ((await self.engine.text_of(NO_LONGER)) or "no longer accepting applications").strip().splitlines()[0]
            return ApplyResult(ApplyOutcome.EXPIRED, f"LinkedIn: {said[:120]}")
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
        if new_page is None and _off_linkedin(page.url, self.base_url):
            new_page = page  # the company site opened in this tab
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
            await self._wait_for_step(modal)
            await self._select_resume(modal, ctx, filler)
            report = await filler.fill(modal)
            unresolved = summarize_unresolved(report)
            all_unresolved.extend(u for u in unresolved if u not in all_unresolved)
            if unresolved and ctx.mode.value != "dry_run":
                out = await ask_and_fill(ctx, page, modal, filler, report.unresolved)
                if out.skipped_by_you:
                    await self._discard(page)
                    return ApplyResult(ApplyOutcome.REJECTED, "You chose to skip this application")
                if not out.answered:
                    await self._discard(page)
                    return ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED,
                                       "Questions need your answers: " + "; ".join(unresolved[:5]),
                                       unresolved=all_unresolved)
                answered = [u for u in unresolved if not any(u.startswith(m + ":") for m in out.still_missing)]
                all_unresolved = [u for u in all_unresolved if u not in answered]
                unresolved = [u for u in unresolved if u not in answered]
            submit = await self.engine.find(SUBMIT_BTN, root=modal)
            if submit is not None:
                return await self._submit(page, modal, submit, ctx, all_unresolved)
            nxt = await self.engine.find(REVIEW_BTN, root=modal) or await self.engine.find(NEXT_BTN, root=modal)
            if nxt is None:
                return await self._navigator_fallback(page, ctx, all_unresolved)
            if unresolved:
                res = await self._need_human(page, ctx, unresolved, all_unresolved)
                if res is not None:
                    return res
            header = await self._step_marker(modal)
            if not await self._advance(page, modal, nxt, header):
                # the step did not move: read what the dialog marks invalid, repair it once, try once more
                problems = await filler.fix_invalid(modal)
                if problems.fixed:
                    await ctx.event("form_repair", "Re-entered: " + "; ".join(problems.fixed[:4]))
                if not problems.fixed or not await self._advance(page, modal, nxt, header):
                    errors = list(dict.fromkeys([*problems.messages, *await collect_errors(modal)]))
                    res = await self._need_human(page, ctx, errors or ["LinkedIn did not accept this step (no message "
                                                                       "the assistant could read)"], all_unresolved)
                    if res is not None:
                        return res
        await self._discard(page)
        return ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED, "Too many Easy Apply steps", unresolved=all_unresolved)

    async def _wait_for_step(self, modal: Locator, timeout_ms: int = 8000) -> None:
        """The dialog opens before its step is rendered: wait for its own Next / Review / Submit button."""
        target = T("easy apply step", css=[*SUBMIT_BTN.css, *REVIEW_BTN.css, *NEXT_BTN.css],
                   role=[*SUBMIT_BTN.role, *REVIEW_BTN.role, *NEXT_BTN.role])
        await self.engine.find(target, root=modal, timeout_ms=timeout_ms)

    async def _navigator_fallback(self, page: Page, ctx: ApplyContext, all_unresolved: list[str]) -> ApplyResult:
        """An Easy Apply step these selectors do not know: let the generic navigator read and continue it."""
        from app.config import get_settings
        from app.navigator.agent import navigate_application

        if not get_settings().navigator_enabled:
            await self._discard(page)
            return ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED, "Unknown Easy Apply step", unresolved=all_unresolved)
        await ctx.event("handoff", "Unfamiliar Easy Apply step: the navigator continues in the dialog", level="warning")
        res = await navigate_application(self.engine, page, ctx, ats_name="linkedin-easy-apply", dialog_only=True)
        if res.outcome != ApplyOutcome.APPLIED and not res.submit_attempted:
            await self._discard(page)  # never leave a half-filled Easy Apply draft behind
        res.unresolved = list(dict.fromkeys([*all_unresolved, *res.unresolved]))
        return res

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
            found, ticked = await modal.first.evaluate(_UNFOLLOW_JS)
            if ticked:
                await ctx.event("follow_company", f"Could not untick 'Follow {ctx.job.company}' - it would follow the company",
                                level="warning")
                unresolved = [*unresolved, f"'Follow {ctx.job.company}' is still ticked (untick it before submitting)"]
        except PlaywrightError:
            pass
        decision = await ctx.decide_submit(page, unresolved=unresolved,
                                           summary=f"LinkedIn Easy Apply for '{ctx.job.title}' at {ctx.job.company} is ready",
                                           detect_submitted=lambda: self._success(page, after_submit=True))
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
            return await self._applied(page) if await self._success(page, after_submit=True) else ApplyResult(
                ApplyOutcome.MANUAL_ACTION_REQUIRED, "Submitted by you, confirmation not detected", submit_attempted=True)
        watcher = SubmissionWatcher(page, portal_probe=lambda: self._confirmation(page, after_submit=True),
                                    error_probe=lambda: self._dialog_errors(modal))
        await watcher.begin()
        try:
            await ctx.mark_submit_attempt()
            await self.engine.click(submit)
            verdict = await watcher.wait(lambda: page, wait_s=20)
            if verdict.state == SubmissionState.NO_EFFECT:  # the click did nothing at all: once more, never again
                await ctx.event("submit_retry", "LinkedIn did not react to 'Submit application' - clicking it once more")
                await self.engine.click(submit)
                verdict = await watcher.wait(lambda: page, wait_s=20)
        finally:
            watcher.stop()  # the LinkedIn tab lives for the whole run: no listener may outlive this submit
        await ctx.event("submission", f"Submission result: {verdict.state.value}"
                        + (f" - {verdict.message}" if verdict.message else ""), evidence=verdict.evidence)
        if verdict.state == SubmissionState.CONFIRMED:
            res = await self._applied(page, seen=verdict.confirmation_text)
            res.evidence = verdict.evidence
            return res
        if verdict.state == SubmissionState.REFUSED:
            return ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED, "LinkedIn rejected the submission: " + verdict.message,
                               submit_attempted=False)
        shot = await self.engine.screenshot("linkedin-unconfirmed")
        if verdict.state == SubmissionState.FAILED:
            return ApplyResult(ApplyOutcome.FAILED, f"Submission failed: {verdict.message}", submit_attempted=True,
                               screenshot=shot, verification="failed", evidence=verdict.evidence)
        return ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED,
                           f"Submitted - confirmation not verified: {verdict.message or 'LinkedIn showed no confirmation'}",
                           submit_attempted=True, screenshot=shot, verification="uncertain", evidence=verdict.evidence)

    async def _advance(self, page: Page, modal: Locator, nxt: Locator, header: str) -> bool:
        """Click Next / Review and report whether the dialog moved to another step."""
        try:
            await self.engine.click(nxt)
        except Exception as exc:  # noqa: BLE001 - a re-rendered button: the step may still have moved
            log.debug("next click: %s", exc)
        await page.wait_for_timeout(400)
        for _ in range(20):
            if await self._step_marker(modal) != header or await self._success(page):
                return True
            await page.wait_for_timeout(250)
        return False

    async def _dialog_errors(self, modal: Locator) -> list[str]:
        try:
            return await collect_errors(modal) if await modal.count() and await modal.is_visible() else []
        except PlaywrightError:
            return []

    async def _applied(self, page: Page, *, seen: str = "") -> ApplyResult:
        """``seen``: the confirmation LinkedIn showed when the submit was verified. A moment later its post-apply
        panel ("Next best action ...") may have replaced it; the record quotes what said the application was sent."""
        text = await self._confirmation(page, after_submit=True)
        dismiss = await self.engine.find(DISMISS, timeout_ms=500)
        if dismiss is not None:
            try:
                await dismiss.click()
            except PlaywrightError:
                pass
        line = confirmation_line(text, seen)
        return ApplyResult(ApplyOutcome.APPLIED, "Applied with LinkedIn Easy Apply",
                           confirmation_text=line[:300], submit_attempted=True, verification="confirmed",
                           evidence=[f"portal_confirmation: {line[:160]}"])

    async def _confirmation(self, page: Page, *, after_submit: bool = False) -> str:
        """LinkedIn's own confirmation text ('' if none): the post-apply dialog / status message, or - only once we
        clicked Submit - the job page's 'Applied ... ago' / 'Application submitted' state."""
        try:
            text = await page.evaluate(CONFIRMATION_JS)
        except PlaywrightError:
            text = ""
        if not text and await self.engine.exists(POST_APPLY, root=page):
            text = await self.engine.text_of(POST_APPLY, root=page) or "Application sent"
        if not text and after_submit and await self.engine.exists(APPLIED_TEXT, root=page):
            text = await self.engine.text_of(APPLIED_TEXT, root=page)
        return (text or "").strip()

    async def _success(self, page: Page, *, after_submit: bool = False) -> bool:
        return bool(await self._confirmation(page, after_submit=after_submit))

    async def _step_marker(self, modal: Locator) -> str:
        """Changes when the dialog moves to another step (old: <progress>; 2026: a '2/6 pages'-style counter)."""
        try:
            return await modal.evaluate(
                "el => { const h = el.querySelector('h3, h2, .t-16'); const p = el.querySelector('progress, [role=progressbar]');"
                " const pages = ((el.innerText || '').match(/\\b\\d+\\s*\\/\\s*\\d+\\s+pages?\\b|\\b\\d+\\s*%/i) || [''])[0];"
                " const labels = Array.from(el.querySelectorAll('label, legend')).map(l => l.innerText.trim()).join('|').slice(0, 300);"
                " return (h ? h.innerText : '') + '|' + (p ? (p.value || p.getAttribute('aria-valuenow')) : '') + '|' + pages"
                " + '|' + el.querySelectorAll('input,select,textarea').length + '|' + labels; }")
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


def _off_linkedin(url: str, base_url: str) -> bool:
    return bool(url) and url.startswith("http") and not url.startswith(base_url) and "linkedin.com" not in url


def _id_from_url(url: str) -> str:
    m = re.search(r"/jobs/view/(\d+)", url or "") or re.search(r"currentJobId=(\d+)", url or "")
    return m.group(1) if m else ""
