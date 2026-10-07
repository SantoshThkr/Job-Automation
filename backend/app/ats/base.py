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
        all_unresolved: list[str] = []

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
            await self.prefill(page, ctx, filler)
            report = await filler.fill(root or page)
            unresolved = summarize_unresolved(report)
            all_unresolved.extend(u for u in unresolved if u not in all_unresolved)
            await ctx.event("step_filled", f"Step {step + 1}: filled {len(report.filled)} field(s), "
                                           f"{len(unresolved)} need review", unresolved=unresolved)

            submit = await self.engine.find(self.submit_target, root=page) if self.submit_target else None
            nxt = await self.engine.find(self.next_target, root=page) if self.next_target else None
            if submit is not None and nxt is None:
                return await self._submit(page, ctx, submit, all_unresolved)
            if nxt is None:
                return await self._manual(page, f"Could not find the next/submit button on {self.display_name}", ctx)

            if unresolved:
                if ctx.mode.value == "dry_run":
                    await ctx.event("dry_run", "Dry run: stopped at a step with unanswered required questions",
                                    unresolved=unresolved)
                    return ApplyResult(ApplyOutcome.DRY_RUN, "Dry run stopped at step with unresolved questions",
                                       ats=self.name, unresolved=all_unresolved)
                res = await ctx.wait_for_human("manual", "Please complete these fields in the browser, then press "
                                               "Continue: " + "; ".join(unresolved[:5]), page=page)
                if res not in (GateResult.RESOLVED, GateResult.APPROVE):
                    return await self._manual(page, "Required questions need your answers: " + "; ".join(unresolved[:5]),
                                              ctx, unresolved=all_unresolved)
            before = await self.engine.page_signature(page)
            await self.engine.click(nxt)
            changed = await self.engine.wait_for_page_change(before, timeout_ms=8000, page=page)
            errors = await collect_errors(page)
            if errors and not changed:
                return await self._manual(page, "Form errors: " + "; ".join(errors[:4]), ctx, unresolved=all_unresolved)
        return await self._manual(page, f"Too many steps on {self.display_name}", ctx)

    async def _submit(self, page: Page, ctx: ApplyContext, submit, unresolved: list[str]) -> ApplyResult:
        decision = await ctx.decide_submit(
            page, unresolved=unresolved,
            summary=f"{self.display_name} application for '{ctx.job.title}' at {ctx.job.company} is filled",
            detect_submitted=lambda: self.confirmation(page))
        if decision == SubmitDecision.DRY_RUN:
            return ApplyResult(ApplyOutcome.DRY_RUN, f"Dry run: {self.display_name} form filled, not submitted",
                               ats=self.name, unresolved=unresolved)
        if decision == SubmitDecision.REJECTED:
            return ApplyResult(ApplyOutcome.REJECTED, "You rejected this application", ats=self.name)
        if decision == SubmitDecision.MANUAL:
            return await self._manual(page, "Not submitted - questions need your input: " + "; ".join(unresolved[:5])
                                      if unresolved else "Not submitted - needs your review", ctx, unresolved=unresolved)
        if decision == SubmitDecision.SUBMITTED_BY_USER:
            text = await self.confirmation(page)
            if text:
                return ApplyResult(ApplyOutcome.APPLIED, "Submitted by you", confirmation_text=text, ats=self.name,
                                   submit_attempted=True)
            return await self._manual(page, "You submitted manually but no confirmation was detected", ctx,
                                      submitted=True)
        await ctx.mark_submit_attempt()
        before = await self.engine.page_signature(page)
        await self.engine.click(submit)
        return await self._await_result(page, ctx, before)

    async def _await_result(self, page: Page, ctx: ApplyContext, before: str, wait_s: float = 20) -> ApplyResult:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_s
        started = loop.time()
        while loop.time() < deadline:
            text = await self.confirmation(page)
            if text:
                return ApplyResult(ApplyOutcome.APPLIED, f"Applied via {self.display_name}",
                                   confirmation_text=text, ats=self.name, submit_attempted=True)
            blockers = await self.engine.detect_blockers(page)
            if blockers:
                res = await ctx.wait_for_human(blockers[0], f"{self.display_name}: please solve the "
                                               f"{blockers[0].upper()} to finish submitting", page=page,
                                               detect_done=lambda: self.confirmation(page))
                if res not in (GateResult.SUBMITTED, GateResult.RESOLVED, GateResult.APPROVE):
                    return await self._manual(page, f"{blockers[0]} blocked submission", ctx, submitted=True)
                deadline = loop.time() + wait_s
                continue
            if loop.time() - started > 3:
                errors = await collect_errors(page)
                if errors and await self.engine.page_signature(page) == before:
                    return await self._manual(page, "Submission rejected by form validation: "
                                              + "; ".join(errors[:4]), ctx, submitted=True)
            await asyncio.sleep(0.5)
        return await self._manual(page, "Submitted but no confirmation was detected - please verify", ctx,
                                  submitted=True)

    async def _manual(self, page: Page, reason: str, ctx: ApplyContext, *, submitted: bool = False,
                      unresolved: list[str] | None = None) -> ApplyResult:
        shot = await self.engine.screenshot(f"{self.name}-manual", page=page)
        return ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED, reason, ats=self.name, submit_attempted=submitted,
                           screenshot=shot, external_url=page.url if not page.is_closed() else None,
                           unresolved=unresolved or [])

    async def _login_wall_present(self, page: Page) -> bool:
        return bool(self.login_wall) and await self.engine.exists(self.login_wall, root=page)  # type: ignore[arg-type]

    async def _login_cleared(self, page: Page) -> bool:
        return not await self._login_wall_present(page)


Detector = Callable[[Page], Awaitable[bool]]
