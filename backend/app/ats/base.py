"""ATS adapter base: a reusable multi-step application engine configured per platform.

Flow: open form -> (login wall?) -> fill step (fast-path + generic filler) -> Next ... -> final submit
gate (dry run / review / auto) -> submit -> confirmation | validation errors | CAPTCHA -> result.
A job is only reported APPLIED when a confirmation page/message is detected.
"""

from __future__ import annotations

import asyncio

import logging
import re
from abc import ABC
from collections.abc import Awaitable, Callable

from playwright.async_api import Error as PlaywrightError, Page

from app.browser.engine import BrowserEngine
from app.browser.forms import FormFiller, collect_errors, summarize_unresolved
from app.browser.selectors import Target
from app.engine.context import ApplyContext, GateResult, SubmitDecision
from app.engine.questions import ask_and_fill
from app.engine.submission import SubmissionState, SubmissionWatcher
from app.portals.common import SUCCESS_TEXT
from app.portals.types import ApplyOutcome, ApplyResult

log = logging.getLogger(__name__)

FastField = tuple[Target, Callable[[ApplyContext], str | None]]


class ATSAdapter(ABC):
    name: str = "ats"
    display_name: str = "ATS"
    url_patterns: tuple[str, ...] = ()
    dom_signals: tuple[str, ...] = ()

    open_targets: tuple[Target, ...] = ()  # clicked (in order, when present) to reach the form
    login_wall: Target | None = None
    form_root: Target | None = None
    next_target: Target | None = None
    submit_target: Target | None = None
    success_url: str | None = None  # regex
    success_targets: tuple[Target, ...] = ()
    fast_fields: tuple[FastField, ...] = ()
    max_steps: int = 10

    def __init__(self, engine: BrowserEngine) -> None:
        self.engine = engine

    @classmethod
    def matches_url(cls, url: str) -> bool:
        return any(re.search(p, url or "", re.I) for p in cls.url_patterns)

    # ---------------------------------------------------------------- hooks
    async def prefill(self, page: Page, ctx: ApplyContext, filler: FormFiller) -> None:
        """Platform-specific fast path (well-known field ids, custom widgets)."""
        for target, value_fn in self.fast_fields:
            value = value_fn(ctx)
            if not value:
                continue
            el = await self.engine.find(target, root=page)
            if el is None:
                continue
            try:
                if (await el.input_value()).strip():
                    continue
                await el.fill(value)
            except PlaywrightError:
                continue

    async def open_form(self, page: Page, ctx: ApplyContext) -> Page:
        """Click through 'Apply' style buttons; returns the page that holds the form."""
        for target in self.open_targets:
            el = await self.engine.find(target, root=page, timeout_ms=2500)
            if el is None:
                continue
            before = await self.engine.page_signature(page)
            new_page = None
            try:
                new_page = await self.engine.expect_popup(lambda: self.engine.click(el), timeout_ms=1500)
            except PlaywrightError:
                pass
            if new_page is not None:
                ctx.engine.use_page(new_page)
                page = new_page
            await self.engine.wait_for_page_change(before, timeout_ms=4000, page=page)
            await self.engine.wait_for_load(page=page)
        return page

    async def confirmation(self, page: Page) -> str:
        try:
            for t in self.success_targets:
                if await self.engine.exists(t, root=page):
                    return await self.engine.text_of(t, root=page) or "Application submitted"
            body = await self.engine.body_text(page, limit=8000)
            m = SUCCESS_TEXT.search(body)
            if m:
                return m.group(0)
            if self.success_url and re.search(self.success_url, page.url, re.I):
                return f"Confirmation page: {page.url}"
        except PlaywrightError:
            return ""
        return ""

    # ----------------------------------------------------------------- flow
    async def apply(self, page: Page, ctx: ApplyContext) -> ApplyResult:
        await ctx.event("ats", f"Applying via {self.display_name}", url=page.url)
        page = await self.open_form(page, ctx)
        filler = ctx.form_filler()
        self._filler = filler
        all_unresolved: list[str] = []
        seen_before: set[str] = set()  # fields of the steps already handled

        for step in range(self.max_steps):
            await self.engine.checkpoint()
            if await self._login_wall_present(page):
                res = await ctx.wait_for_human(
                    "login", f"{self.display_name} requires you to sign in / create an account for this employer",
                    page=page, detect_done=lambda: self._login_cleared(page))
                if res not in (GateResult.RESOLVED, GateResult.APPROVE):
                    return await self._manual(page, f"{self.display_name} account sign-in required", ctx)
            blockers = await self.engine.detect_blockers(page)
            if blockers:
                res = await ctx.wait_for_human(blockers[0], f"{self.display_name} shows a {blockers[0].upper()}",
                                               page=page)
                if res not in (GateResult.RESOLVED, GateResult.APPROVE):
                    return await self._manual(page, f"{blockers[0]} required before the form can be filled", ctx)

            root = (await self.engine.find(self.form_root, root=page, timeout_ms=3000)) if self.form_root else None
            if step:
                # the next step is still being drawn when the page first changes
                await self._settle(root or page, seen_before)
            await self.prefill(page, ctx, filler)
            report = await filler.fill(root or page)
            seen_before |= {i.field.id for i in report.items}
            unresolved = summarize_unresolved(report)
            all_unresolved.extend(u for u in unresolved if u not in all_unresolved)
            await ctx.event("step_filled", f"Step {step + 1}: filled {len(report.filled)} field(s), "
                                           f"{len(unresolved)} need review", unresolved=unresolved)

            submit = await self.engine.find(self.submit_target, root=page) if self.submit_target else None
            nxt = await self.engine.find(self.next_target, root=page) if self.next_target else None
            if not report.items and not report.resume_uploaded and step == 0:
                # no application fields at all: this is not the form this adapter knows (wrong page, SSO, a
                # different platform) - never press its submit button; the navigator reads the page instead
                return await self._manual(page, f"No {self.display_name} application form on this page", ctx,
                                          handoff=True)
            if submit is not None and nxt is None:
                if unresolved and ctx.mode.value != "dry_run":
                    out = await ask_and_fill(ctx, page, root or page, filler, report.unresolved)
                    if out.skipped_by_you:
                        return ApplyResult(ApplyOutcome.REJECTED, "You chose to skip this application", ats=self.name)
                    if out.answered:
                        answered = [u for u in unresolved if not any(u.startswith(m + ":") for m in out.still_missing)]
                        all_unresolved = [u for u in all_unresolved if u not in answered]
                return await self._submit(page, ctx, submit, all_unresolved)
            if nxt is None:
                return await self._manual(page, f"Could not find the next/submit button on {self.display_name}", ctx,
                                          handoff=True)

            if unresolved:
                if ctx.mode.value == "dry_run":
                    await ctx.event("dry_run", "Dry run: stopped at a step with unanswered required questions",
                                    unresolved=unresolved)
                    return ApplyResult(ApplyOutcome.DRY_RUN, "Dry run stopped at step with unresolved questions",
                                       ats=self.name, unresolved=all_unresolved)
                out = await ask_and_fill(ctx, page, root or page, filler, report.unresolved)
                if out.skipped_by_you:
                    return ApplyResult(ApplyOutcome.REJECTED, "You chose to skip this application", ats=self.name)
                if not out.answered:
                    return await self._manual(page, "Required questions need your answers: " + "; ".join(unresolved[:5]),
                                              ctx, unresolved=all_unresolved)
                answered = [u for u in unresolved if not any(u.startswith(m + ":") for m in out.still_missing)]
                all_unresolved = [u for u in all_unresolved if u not in answered]
            before = await self.engine.page_signature(page)
            await self.engine.click(nxt)
            changed = await self.engine.wait_for_page_change(before, timeout_ms=8000, page=page)
            errors = await collect_errors(page)
            if errors and not changed:
                return await self._manual(page, "Form errors: " + "; ".join(errors[:4]), ctx, unresolved=all_unresolved)
        return await self._manual(page, f"Too many steps on {self.display_name}", ctx, handoff=True)

    async def _settle(self, root, known: set[str], timeout_s: float = 6.0) -> int:
        """Wait until this step's own fields are there and no longer changing: fields that were not part of the
        earlier steps, the same ones in two scans in a row. A step read while it was still rendering looked empty -
        and an empty step with a Submit button was submitted (SmartRecruiters, run #29: four questions and a
        required consent box were never seen; the hidden upload boxes of step 1 were all the scan found).
        A step that really has no fields of its own (a review page) costs the full wait once."""
        from app.browser.forms import scan_fields

        last: frozenset[str] = frozenset()
        waited = 0.0
        while waited < timeout_s:
            fresh = frozenset(f.id for f in await scan_fields(root) if f.id not in known)
            if fresh and fresh == last:
                return len(fresh)
            last = fresh
            await asyncio.sleep(0.5)
            waited += 0.5
        return len(last)

    async def _submit(self, page: Page, ctx: ApplyContext, submit, unresolved: list[str]) -> ApplyResult:
        decision = await ctx.decide_submit(
            page, unresolved=unresolved,
            summary=f"{self.display_name} application for '{ctx.job.title}' at {ctx.job.company} is filled",
            detect_submitted=lambda: self.ats_confirmation(page))
        if decision == SubmitDecision.DRY_RUN:
            return ApplyResult(ApplyOutcome.DRY_RUN, f"Dry run: {self.display_name} form filled, not submitted",
                               ats=self.name, unresolved=unresolved)
        if decision == SubmitDecision.REJECTED:
            return ApplyResult(ApplyOutcome.REJECTED, "You rejected this application", ats=self.name)
        if decision == SubmitDecision.MANUAL:
            return await self._manual(page, "Not submitted - questions need your input: " + "; ".join(unresolved[:5])
                                      if unresolved else "Not submitted - needs your review", ctx, unresolved=unresolved)
        if decision == SubmitDecision.SUBMITTED_BY_USER:
            text = await self.ats_confirmation(page)
            if text:
                return ApplyResult(ApplyOutcome.APPLIED, "Submitted by you", confirmation_text=text, ats=self.name,
                                   submit_attempted=True)
            return await self._manual(page, "You submitted manually but no confirmation was detected", ctx,
                                      submitted=True)
        watcher = SubmissionWatcher(page, portal_probe=lambda: self.ats_confirmation(page))
        await watcher.begin()
        self._watchers = [watcher]
        try:
            await ctx.mark_submit_attempt()
            before = await self.engine.page_signature(page)
            await self.engine.click(submit)
            return await self._await_result(page, ctx, before, submit=submit, watcher=watcher)
        finally:
            for w in self._watchers:  # including one created for a repaired resubmit
                w.stop()
            self._watchers = []

    async def ats_confirmation(self, page: Page) -> str:
        """This ATS's own confirmation: its confirmation element or its confirmation route (no free-text guessing)."""
        try:
            for t in self.success_targets:
                if await self.engine.exists(t, root=page):
                    return await self.engine.text_of(t, root=page) or f"{self.display_name} confirmation"
            if self.success_url and re.search(self.success_url, page.url, re.I):
                return f"{self.display_name} confirmation page: {page.url}"
        except PlaywrightError:
            return ""
        return ""

    async def _await_result(self, page: Page, ctx: ApplyContext, before: str, wait_s: float = 20, *,
                            submit=None, watcher: SubmissionWatcher | None = None) -> ApplyResult:
        """SUBMIT_CLICKED -> WAITING -> CONFIRMED / UNCERTAIN / FAILED via the central watcher; a refused submit gets
        one format repair (same fact, e.g. 10-digit phone) and one more click - never a blind resubmit."""
        assert watcher is not None
        repaired = False
        rounds = 0
        while True:
            verdict = await watcher.wait(lambda: ctx.engine.page if not ctx.engine.page.is_closed() else page,
                                         wait_s=wait_s)
            blockers = await self.engine.detect_blockers(page) if not page.is_closed() else []
            if verdict.state == SubmissionState.BLOCKED and "captcha" not in blockers:
                blockers = ["captcha", *blockers]
            if blockers and verdict.state != SubmissionState.CONFIRMED:
                rounds += 1
                if rounds > 2:  # never loop on a challenge only you can complete
                    return await self._manual(page, f"{blockers[0]} blocked submission", ctx, submitted=True)
                watcher.captcha_requests = 0
                res = await ctx.wait_for_human(blockers[0], f"{self.display_name}: please solve the "
                                               f"{blockers[0].upper()} to finish submitting", page=page,
                                               detect_done=lambda: self.ats_confirmation(page))
                if res not in (GateResult.SUBMITTED, GateResult.RESOLVED, GateResult.APPROVE):
                    watcher.stop()
                    return await self._manual(page, f"{blockers[0]} blocked submission", ctx, submitted=True)
                continue
            filler = getattr(self, "_filler", None)
            if (verdict.state == SubmissionState.REFUSED and not repaired and submit is not None and filler is not None
                    and await filler.repair(page, [verdict.message])):
                repaired = True
                await ctx.event("form_repair", f"{self.display_name} rejected a value format: re-entered it")
                watcher.stop()
                watcher = SubmissionWatcher(page, submit_selector="", portal_probe=lambda: self.ats_confirmation(page))
                getattr(self, "_watchers", []).append(watcher)
                await watcher.begin()
                await ctx.mark_submit_attempt()
                await self.engine.click(submit)
                continue
            break
        watcher.stop()
        await ctx.event("submission", f"Submission result: {verdict.state.value}"
                        + (f" - {verdict.message}" if verdict.message else ""), evidence=verdict.evidence)
        if verdict.state == SubmissionState.CONFIRMED:
            return ApplyResult(ApplyOutcome.APPLIED, f"Applied via {self.display_name}",
                               confirmation_text=verdict.confirmation_text, ats=self.name, submit_attempted=True,
                               verification="confirmed", evidence=verdict.evidence)
        if verdict.state == SubmissionState.REFUSED:
            res = await self._manual(page, "Submission rejected by form validation: " + verdict.message, ctx,
                                     submitted=False)
            res.nothing_sent = True  # the form's own validation stopped it: Retry is safe, nothing can be sent twice
            return res
        detail = verdict.message or ", ".join(verdict.evidence) or "no confirmation the assistant could read"
        res = await self._manual(page, (f"Submission failed: {detail}" if verdict.state == SubmissionState.FAILED
                                        else f"Submitted - confirmation not verified: {detail}"), ctx, submitted=True)
        if verdict.state == SubmissionState.FAILED:
            res.outcome = ApplyOutcome.FAILED
        res.verification, res.evidence = verdict.verification or "uncertain", verdict.evidence
        return res

    async def _manual(self, page: Page, reason: str, ctx: ApplyContext, *, submitted: bool = False,
                      unresolved: list[str] | None = None, handoff: bool = False) -> ApplyResult:
        shot = await self.engine.screenshot(f"{self.name}-manual", page=page)
        return ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED, reason, ats=self.name, submit_attempted=submitted,
                           screenshot=shot, external_url=page.url if not page.is_closed() else None,
                           unresolved=unresolved or [], handoff=handoff and not submitted)

    async def _login_wall_present(self, page: Page) -> bool:
        return bool(self.login_wall) and await self.engine.exists(self.login_wall, root=page)  # type: ignore[arg-type]

    async def _login_cleared(self, page: Page) -> bool:
        return not await self._login_wall_present(page)


Detector = Callable[[Page], Awaitable[bool]]
