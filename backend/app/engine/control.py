"""DB-backed control channel between the dashboard/API and the agent process.

The dashboard writes the *desired* state (RUNNING / PAUSED / STOPPED) and gate responses;
the agent polls them at every checkpoint and publishes its *actual* state + heartbeat.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass

from playwright.async_api import Page

from app.browser.errors import StopRequested
from app.config import get_settings
from app.db import session_scope
from app.engine.context import Detector, GateResult
from app.engine.recorder import add_event, set_status
from app.models import ActualState, AgentState, Application, DesiredState, Job, JobStatus, utcnow

log = logging.getLogger(__name__)

HEARTBEAT_STALE_S = 20


@dataclass
class ControlSnapshot:
    desired: str
    mode: str
    portals: list[str]
    gate_response: str | None


class AgentControl:
    def __init__(self, cache_s: float = 0.4) -> None:
        self._cache_s = cache_s
        self._snap: ControlSnapshot | None = None
        self._snap_at = 0.0
        self._last_beat = 0.0
        self._paused = False

    def snapshot(self, fresh: bool = False) -> ControlSnapshot:
        now = time.monotonic()
        if not fresh and self._snap is not None and now - self._snap_at < self._cache_s:
            return self._snap
        with session_scope() as s:
            st = AgentState.get(s)
            self._snap = ControlSnapshot(st.desired_state, st.mode, list(st.portals or []), st.gate_response)
        self._snap_at = now
        return self._snap

    def update(self, **fields) -> None:
        with session_scope() as s:
            st = AgentState.get(s)
            for k, v in fields.items():
                setattr(st, k, v)
            st.heartbeat_at = utcnow()
        self._last_beat = time.monotonic()

    def set_actual(self, state: ActualState | str, activity: str | None = None, message: str | None = None) -> None:
        fields: dict = {"actual_state": str(state)}
        if activity is not None:
            fields["current_activity"] = activity
        if message is not None:
            fields["message"] = message
        self.update(**fields)

    def activity(self, text: str) -> None:
        self.update(current_activity=text[:500])

    def heartbeat(self) -> None:
        if time.monotonic() - self._last_beat > 3:
            self.update(pid=os.getpid())

    async def checkpoint(self) -> None:
        """Called before every browser action: blocks while paused, raises StopRequested on stop."""
        while True:
            snap = self.snapshot()
            if snap.desired == DesiredState.STOPPED:
                raise StopRequested()
            if snap.desired == DesiredState.PAUSED:
                if not self._paused:
                    self._paused = True
                    self.set_actual(ActualState.PAUSED, message="Paused by user")
                    log.info("Agent paused")
                self.heartbeat()
                await asyncio.sleep(1.0)
                continue
            if self._paused:
                self._paused = False
                self.set_actual(ActualState.RUNNING, message="Resumed")
                log.info("Agent resumed")
            self.heartbeat()
            return


_KIND_STATE = {"login": ActualState.WAITING_LOGIN, "review": ActualState.WAITING_REVIEW}


class DBHumanGate:
    """Waits for the user (dashboard button or action in the browser) without blocking stop/pause."""

    def __init__(self, control: AgentControl, job_id: int | None = None, application_id: int | None = None,
                 poll_s: float = 1.5) -> None:
        self.control = control
        self.job_id = job_id
        self.application_id = application_id
        self.poll_s = poll_s

    async def wait(self, kind: str, reason: str, *, page: Page | None = None, detect_done: Detector | None = None,
                   timeout_s: int | None = None) -> GateResult:
        settings = get_settings()
        if timeout_s is None:
            timeout_s = settings.login_wait_timeout_s if kind == "login" else settings.review_wait_timeout_s
        with session_scope() as s:
            st = AgentState.get(s)
            st.gate_kind, st.gate_reason, st.gate_response = kind, reason[:2000], None
            st.gate_opened_at = utcnow()
            st.pending_job_id = self.job_id
            st.actual_state = _KIND_STATE.get(kind, ActualState.WAITING_MANUAL)
            st.message = reason[:2000]
            if self.job_id is not None:
                job = s.get(Job, self.job_id)
                if job is not None:
                    if kind == "review":
                        set_status(s, job, JobStatus.AWAITING_APPROVAL, reason)
                    add_event(s, job.id, f"waiting_{kind}", reason, application_id=self.application_id,
                              level="warning" if kind != "review" else "info")
        log.info("Waiting for user (%s): %s", kind, reason)
        deadline = time.monotonic() + timeout_s
        result = GateResult.TIMEOUT
        try:
            while time.monotonic() < deadline:
                await self.control.checkpoint()
                snap = self.control.snapshot(fresh=True)
                if snap.gate_response == "approve":
                    result = GateResult.APPROVE if kind == "review" else GateResult.RESOLVED
                    break
                if snap.gate_response == "reject":
                    result = GateResult.REJECT
                    break
                if detect_done is not None:
                    try:
                        if await detect_done():
                            result = GateResult.SUBMITTED if kind == "review" else GateResult.RESOLVED
                            break
                    except Exception as exc:  # page navigated/closed while checking
                        log.debug("gate detector error: %s", exc)
                await asyncio.sleep(self.poll_s)
        except StopRequested:
            self._close(None)
            raise
        self._close(result)
        log.info("Gate %s finished: %s", kind, result)
        return result

    def _close(self, result: GateResult | None) -> None:
        with session_scope() as s:
            st = AgentState.get(s)
            st.gate_kind = None
            st.gate_reason = ""
            st.gate_response = None
            st.gate_opened_at = None
            st.pending_job_id = None
            if st.desired_state == DesiredState.RUNNING:
                st.actual_state = ActualState.RUNNING
            if self.application_id is not None and result is not None:
                app = s.get(Application, self.application_id)
                if app is not None and result in (GateResult.APPROVE, GateResult.REJECT):
                    app.review_decision = "approve" if result == GateResult.APPROVE else "reject"


def agent_is_alive(st: AgentState) -> bool:
    if st.pid is None or st.heartbeat_at is None:
        return False
    if (utcnow() - st.heartbeat_at).total_seconds() > HEARTBEAT_STALE_S:
        return False
    try:
        os.kill(st.pid, 0)
    except OSError:
        return False
    return st.actual_state not in {ActualState.STOPPED, ActualState.IDLE, ActualState.ERROR}
