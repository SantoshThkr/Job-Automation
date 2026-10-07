"""Helpers for adapter tests: a scriptable human gate and an ApplyContext factory."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from app.answers.answerer import Answerer
from app.engine.context import ApplyContext, GateResult
from app.models import AgentMode
from app.portals.types import JobPosting


class FakeGate:
    """Simulates the user. Each response is a GateResult or an async callable(page) -> GateResult."""

    def __init__(self, responses: list | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[tuple[str, str]] = []

    async def wait(self, kind, reason, *, page=None, detect_done=None, timeout_s=None) -> GateResult:
        self.calls.append((kind, reason))
        if not self.responses:
            return GateResult.TIMEOUT
        r = self.responses.pop(0)
        if callable(r):
            r = await r(page)
        return r


def make_ctx(engine, profile, job: JobPosting, mode: AgentMode = AgentMode.AUTO, gate: FakeGate | None = None,
             assistant=None, submit_blockers: list[str] | None = None) -> ApplyContext:
    events: list[tuple[str, str]] = []
    submits: list[bool] = []

    async def log_event(type_, message, level="info", data=None):
        events.append((type_, message))

    async def on_submit():
        submits.append(True)

    ctx = ApplyContext(engine=engine, profile=profile, answerer=Answerer(profile, assistant), mode=mode, job=job,
                       resume_path=profile.resumes[0].absolute_path, gate=gate or FakeGate(), log_event=log_event,
                       on_submit_attempt=on_submit, submit_blockers=submit_blockers or [])
    ctx.events = events  # type: ignore[attr-defined]
    ctx.submits = submits  # type: ignore[attr-defined]
    return ctx


Action = Callable[[object], Awaitable[GateResult]]
