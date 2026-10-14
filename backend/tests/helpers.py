"""Helpers for adapter tests: a scriptable human gate and an ApplyContext factory."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from app.answers.answerer import Answerer
from app.engine.context import ApplyContext, GateResult
from app.models import AgentMode
from app.portals.types import JobPosting


class FakeGate:
    """Simulates the user. Each response is a GateResult, an async callable(page) -> GateResult, or a callable
    (payload) -> dict of answers (the user answering questions in the dashboard)."""

    def __init__(self, responses: list | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[tuple[str, str]] = []
        self.payloads: list[dict | None] = []
        self.detectors: list = []  # how the agent would notice by itself that you are done (None = it cannot)
        self.last_answers: dict[str, str] = {}

    async def wait(self, kind, reason, *, page=None, detect_done=None, timeout_s=None, payload=None) -> GateResult:
        self.calls.append((kind, reason))
        self.payloads.append(payload)
        self.detectors.append(detect_done)
        self.last_answers = {}
        if not self.responses:
            return GateResult.TIMEOUT
        r = self.responses.pop(0)
        if isinstance(r, dict):
            self.last_answers = dict(r)
            return GateResult.RESOLVED
        if callable(r) and getattr(r, "answers_from_payload", False):
            self.last_answers = r(payload)
            return GateResult.RESOLVED
        if callable(r):
            r = await r(page)
        return r


def answers_by_question(mapping: dict[str, str]):
    """Gate response that answers dashboard questions by (partial, case-insensitive) question text."""

    def respond(payload):
        out = {}
        for q in (payload or {}).get("questions", []):
            for needle, value in mapping.items():
                if needle.lower() in q["question"].lower():
                    out[q["id"]] = value
        return out

    respond.answers_from_payload = True  # type: ignore[attr-defined]
    return respond


def make_ctx(engine, profile, job: JobPosting, mode: AgentMode = AgentMode.AUTO, gate: FakeGate | None = None,
             assistant=None, submit_blockers: list[str] | None = None, auto_wait: int = 0) -> ApplyContext:
    events: list[tuple[str, str]] = []
    submits: list[bool] = []

    async def log_event(type_, message, level="info", data=None):
        events.append((type_, message))

    async def on_submit():
        submits.append(True)

    remembered: list[tuple[str, str]] = []

    async def remember(question, answer):
        remembered.append((question, answer))

    ctx = ApplyContext(engine=engine, profile=profile, answerer=Answerer(profile, assistant), mode=mode, job=job,
                       resume_path=profile.resumes[0].absolute_path, gate=gate or FakeGate(), log_event=log_event,
                       on_submit_attempt=on_submit, submit_blockers=submit_blockers or [], remember=remember,
                       auto_human_wait_s=auto_wait)
    ctx.events = events  # type: ignore[attr-defined]
    ctx.submits = submits  # type: ignore[attr-defined]
    ctx.remembered = remembered  # type: ignore[attr-defined]
    return ctx


Action = Callable[[object], Awaitable[GateResult]]
