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
from app.config import get_settings
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
    """Waits for the user. ``payload`` carries structured data (e.g. questions to answer in the dashboard);
    answers the user typed there are exposed afterwards as ``last_answers``."""

    last_answers: dict[str, str]

    async def wait(self, kind: str, reason: str, *, page: Page | None = None,
                   detect_done: Detector | None = None, timeout_s: int | None = None,
                   payload: dict | None = None) -> GateResult: ...


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
    # the last check before the irreversible click: '' or why this job must not be applied to now (its age)
    recency_check: Callable[[Page | None], Awaitable[str]] | None = None
    recency_blocked: str = ""
    answers_log: list[tuple[QuestionContext, Answer]] = field(default_factory=list)
    on_step: Callable[[str, str], Awaitable[None]] | None = None  # (description, url) -> dashboard "current step"
    remember: Callable[[str, str], Awaitable[None]] | None = None  # save a user answer for future forms
    auto_human_wait_s: int | None = None  # None = settings.auto_human_wait_s
    approved: bool = False  # review mode: you approved this application (later submit steps need no re-approval)
    last_gate: GateResult | None = None
    _asked_logged: set[str] = field(default_factory=set)

    @property
    def portal(self) -> str:
        return self.job.portal

    def form_filler(self) -> FormFiller:
        return FormFiller(self.engine, self.answerer, resume_path=self.resume_path, job=self.job.to_job_input(),
                          portal=self.job.portal, on_answer=self._on_answer, on_result=self._on_result)

    async def _on_result(self, item) -> None:
        """After a field was answered, filled and checked: one readable entry in the activity log."""
        if item.answer is None or item.status in {"prefilled", "uploaded"}:
            return
        if item.answer.mode in {"skipped", "not_a_question"} and not item.field.required:
            return
        await self.log_question(item.question, item.answer, filled=item.status in {"filled", "unresolved"},
                                verified=item.verified, problem=item.note if item.status == "failed" else "")
        if self.record_answer is not None and item.answer.verified is not None:
            q = QuestionContext(question=item.question, field_type=item.field.type, options=item.field.option_labels,
                                required=item.field.required, job=self.job.to_job_input(), portal=self.portal)
            await self.record_answer(item.field, q, item.answer)

    async def log_question(self, question: str, ans: Answer, *, filled: bool, verified: bool | None,
                           problem: str = "") -> None:
        from app.answers.answerer import MODE_LABEL, describe_sources

        lines = [f"Question detected: {question[:200]}"]
        if ans.mode == "ask" or ans.value is None:
            if question in self._asked_logged:
                return  # already reported for this application
            self._asked_logged.add(question)
            lines += [ans.note or "No verified information available", "Manual input required"]
            level = "warning"
        else:
            lines += [f"Answer: {ans.value[:160]}{'...' if len(ans.value) > 160 else ''}",
                      f"Answer source: {describe_sources(ans.sources)}",
                      f"Answer mode: {MODE_LABEL.get(ans.mode, ans.mode or ans.source)}",
                      f"Confidence: {ans.confidence:.2f}"]
            if problem:
                lines.append(f"Not accepted by the form: {problem}")
            elif filled:
                lines.append("Filled answer · " + ("Verified" if verified else "could not verify the value"))
            if ans.needs_review:
                lines.append("Low confidence - you will be asked to confirm")
            level = "warning" if problem or ans.needs_review else "info"
        await self.log_event("question", "\n".join(lines), level=level,
                             data={"category": ans.category, "mode": ans.mode, "sources": ans.sources[:12],
                                   "confidence": round(ans.confidence, 3), "verified": verified})

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

    async def step(self, description: str, url: str = "", **data: Any) -> None:
        """A visible step of the application (shown live in the dashboard and in the job timeline)."""
        log.info("[%s] step: %s", self.job.portal, description)
        if self.on_step is not None:
            try:
                await self.on_step(description, url)
            except Exception as exc:  # the dashboard is informational: never break an application
                log.debug("on_step failed: %s", exc)
        await self.log_event("step", description, level="info", data={"url": url, **data})

    async def save_user_answer(self, question: str, answer: str) -> None:
        if self.remember is not None and question.strip() and answer.strip():
            await self.remember(question, answer)

    async def ask_questions(self, questions: list[dict[str, Any]], page: Page | None = None,
                            *, intro: str = "", detect_done: Detector | None = None) -> dict[str, str] | None:
        """Ask you (in the dashboard) to answer questions the assistant could not answer from your profile.

        Returns {question id: answer} for answers typed in the dashboard ({} when you completed the fields
        directly in the browser and pressed Continue), or None when nobody answered / Dry Run.
        """
        if self.mode == AgentMode.DRY_RUN or not questions:
            return None
        from app.answers.options import choice_labels

        # every question shown in the dashboard lists each choice once (whatever the page's markup repeated)
        questions = [{**q, "options": choice_labels(q.get("options"))} for q in questions]
        names = "; ".join(str(q.get("question", ""))[:80] for q in questions[:4])
        reason = intro or f"{len(questions)} question(s) need your answer"
        reason = f"{reason}: {names}"
        await self.step(f"Waiting for you: {reason}", page.url if page else "", kind="questions")
        res = await self.wait_for_human("questions", reason, page=page, detect_done=detect_done,
                                        payload={"questions": questions, "url": page.url if page else "",
                                                 "job": f"{self.job.title} @ {self.job.company}"})
        if res in (GateResult.RESOLVED, GateResult.APPROVE):
            return dict(getattr(self.gate, "last_answers", None) or {})
        return None

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
        if self.recency_check is not None:
            why = await self.recency_check(page)
            if why:
                self.recency_blocked = why
                await self.event("recency", f"Not submitting: {why}", level="warning")
                return SubmitDecision.DRY_RUN if self.mode == AgentMode.DRY_RUN else SubmitDecision.MANUAL
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
        if self.approved and not unresolved:
            return SubmitDecision.SUBMIT  # multi-submit flows: you already approved this application
        reason = summary or "Application is ready - approve to submit"
        if unresolved:
            reason += f". Please complete in the browser: {'; '.join(unresolved[:5])}"
        res = await self.gate.wait("review", reason, page=page, detect_done=detect_submitted)
        if res in (GateResult.APPROVE, GateResult.RESOLVED):
            self.approved = True
        return {
            GateResult.APPROVE: SubmitDecision.SUBMIT,
            GateResult.RESOLVED: SubmitDecision.SUBMIT,
            GateResult.REJECT: SubmitDecision.REJECTED,
            GateResult.SUBMITTED: SubmitDecision.SUBMITTED_BY_USER,
            GateResult.TIMEOUT: SubmitDecision.MANUAL,
        }[res]

    async def wait_for_human(self, kind: str, reason: str, *, page: Page | None = None,
                             detect_done: Detector | None = None, timeout_s: int | None = None,
                             payload: dict | None = None) -> GateResult:
        """CAPTCHA / OTP / sign-in / questions: pause, tell you what is needed, continue from the same page.

        In Auto Apply the wait is bounded (PJA_AUTO_HUMAN_WAIT_S) so one job cannot stall the run; when
        nobody responds the job is parked as "needs you" and the run moves on.
        """
        if self.mode == AgentMode.AUTO and kind not in {"login"}:
            wait_s = get_settings().auto_human_wait_s if self.auto_human_wait_s is None else self.auto_human_wait_s
            if wait_s <= 0:
                self.last_gate = GateResult.TIMEOUT
                await self.event("manual_required", reason, level="warning", kind=kind)
                return GateResult.TIMEOUT
            timeout_s = min(timeout_s, wait_s) if timeout_s else wait_s
        kwargs: dict[str, Any] = {"page": page, "detect_done": detect_done, "timeout_s": timeout_s}
        if payload is not None:
            kwargs["payload"] = payload
        res = await self.gate.wait(kind, reason, **kwargs)
        self.last_gate = res
        if res == GateResult.TIMEOUT:
            await self.event("manual_required", f"No response: {reason}", level="warning", kind=kind)
        return res
