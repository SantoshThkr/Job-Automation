"""ApplyContext: everything an adapter needs while applying, including the submit gate that
implements Dry Run / Review / Auto Apply semantics in one place."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from playwright.async_api import Page

from app.answers.answerer import Answerer
from app.answers.question_bank import Answer, QuestionContext
from app.browser.engine import BrowserEngine
from app.browser.forms import FormField, FormFiller
from app.models import AgentMode
from app.portals.types import JobPosting
from app.profile import Profile

log = logging.getLogger(__name__)


class SubmitDecision(StrEnum):
    SUBMIT = "submit"
    DRY_RUN = "dry_run"
    REJECTED = "rejected"
    SUBMITTED_BY_USER = "submitted_by_user"
    MANUAL = "manual"


class GateResult(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    SUBMITTED = "submitted"  # the user submitted by hand in the browser
    RESOLVED = "resolved"  # e.g. login / CAPTCHA completed
    TIMEOUT = "timeout"


Detector = Callable[[], Awaitable[bool]]


class HumanGate(Protocol):
    async def wait(self, kind: str, reason: str, *, page: Page | None = None,
                   detect_done: Detector | None = None, timeout_s: int | None = None) -> GateResult: ...


EventFn = Callable[..., Awaitable[None]]


async def _noop_event(*_a: Any, **_k: Any) -> None:
    return None


@dataclass
class ApplyContext:
    engine: BrowserEngine
    profile: Profile
    answerer: Answerer
    mode: AgentMode
    job: JobPosting
    resume_path: Path | None
    gate: HumanGate
    log_event: EventFn = _noop_event
    record_answer: Callable[[FormField | None, QuestionContext, Answer], Awaitable[None]] | None = None
    on_submit_attempt: Callable[[], Awaitable[None]] | None = None
    submit_blockers: list[str] = field(default_factory=list)
    answers_log: list[tuple[QuestionContext, Answer]] = field(default_factory=list)

    @property
    def portal(self) -> str:
        return self.job.portal

    def form_filler(self) -> FormFiller:
        return FormFiller(self.engine, self.answerer, resume_path=self.resume_path, job=self.job.to_job_input(),
                          portal=self.job.portal, on_answer=self._on_answer)

    async def _on_answer(self, f: FormField | None, q: QuestionContext, a: Answer) -> None:
        self.answers_log.append((q, a))
        if self.record_answer is not None:
            await self.record_answer(f, q, a)

    async def answer(self, q: QuestionContext) -> Answer:
        """Answer a question outside the generic form filler (e.g. a portal chatbot) and record it."""
        a = await self.answerer.answer(q)
        await self._on_answer(None, q, a)
        return a

    async def event(self, type_: str, message: str, level: str = "info", **data: Any) -> None:
        log.info("[%s] %s: %s", self.job.portal, type_, message)
        await self.log_event(type_, message, level=level, data=data)

    async def mark_submit_attempt(self) -> None:
        if self.on_submit_attempt is not None:
            await self.on_submit_attempt()

    async def decide_submit(self, page: Page | None, *, unresolved: list[str], summary: str = "",
                            detect_submitted: Detector | None = None) -> SubmitDecision:
        """Central policy before the irreversible click.

        * dry_run: never submit.
        * auto:    submit only if every required answer is confident and the profile is complete;
                   otherwise MANUAL_ACTION_REQUIRED (the run continues with the next job).
        * review:  pause and wait for Approve / Reject from the dashboard (the user may also fix
                   fields or submit by hand in the open browser window).
        """
        if self.mode == AgentMode.DRY_RUN:
            await self.event("dry_run", "Dry run: stopped before submitting"
                             + (f" ({len(unresolved)} question(s) would need review)" if unresolved else ""),
                             unresolved=unresolved)
            return SubmitDecision.DRY_RUN
        if self.submit_blockers:
            await self.event("blocked", "Profile incomplete - not submitting: " + "; ".join(self.submit_blockers),
                             level="warning")
            return SubmitDecision.MANUAL
        if self.mode == AgentMode.AUTO:
            if not unresolved:
                return SubmitDecision.SUBMIT
            await self.event("needs_review", f"{len(unresolved)} question(s) need your input - not auto-submitting",
                             level="warning", unresolved=unresolved)
            return SubmitDecision.MANUAL
        reason = summary or "Application is ready - approve to submit"
        if unresolved:
            reason += f". Please complete in the browser: {'; '.join(unresolved[:5])}"
        res = await self.gate.wait("review", reason, page=page, detect_done=detect_submitted)
        return {
            GateResult.APPROVE: SubmitDecision.SUBMIT,
            GateResult.RESOLVED: SubmitDecision.SUBMIT,
            GateResult.REJECT: SubmitDecision.REJECTED,
            GateResult.SUBMITTED: SubmitDecision.SUBMITTED_BY_USER,
            GateResult.TIMEOUT: SubmitDecision.MANUAL,
        }[res]

    async def wait_for_human(self, kind: str, reason: str, *, page: Page | None = None,
                             detect_done: Detector | None = None, timeout_s: int | None = None) -> GateResult:
        """CAPTCHA / OTP / mid-form manual input. In auto mode we do not block the run."""
        if self.mode == AgentMode.AUTO and kind not in {"login"}:
            await self.event("manual_required", reason, level="warning", kind=kind)
            return GateResult.TIMEOUT
        return await self.gate.wait(kind, reason, page=page, detect_done=detect_done, timeout_s=timeout_s)
