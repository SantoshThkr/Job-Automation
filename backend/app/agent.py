"""Browser agent process.

Started by the dashboard's Start button (the API spawns ``python -m app.agent``) or manually:

    python -m app.agent --mode dry_run --portals naukri,linkedin
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys

from app.ai.job_assistant import JobAssistant
from app.browser.engine import BrowserEngine
from app.config import get_settings
from app.db import init_db, session_scope
from app.engine.control import AgentControl, agent_is_alive
from app.engine.orchestrator import Orchestrator
from app.logging_config import setup_logging
from app.models import ActualState, AgentMode, AgentState, DesiredState, utcnow

log = logging.getLogger("app.agent")


class AgentLock:
    """Single-instance lock (one browser profile can only be used by one process)."""

    def __init__(self) -> None:
        self.path = get_settings().data_path / "agent.lock"

    def acquire(self) -> bool:
        if self.path.exists():
            try:
                pid = int(self.path.read_text().strip() or 0)
            except ValueError:
                pid = 0
            if pid and pid != os.getpid():
                try:
                    os.kill(pid, 0)
                    return False  # another live agent
                except OSError:
                    pass  # stale lock
        self.path.write_text(str(os.getpid()))
        return True

    def release(self) -> None:
        with contextlib.suppress(OSError):
            if self.path.exists() and self.path.read_text().strip() == str(os.getpid()):
                self.path.unlink()


async def _heartbeat(control: AgentControl) -> None:
    while True:
        try:
            control.update(pid=os.getpid())
        except Exception as exc:  # DB briefly locked etc.
            log.debug("heartbeat failed: %s", exc)
        await asyncio.sleep(5)


async def run_agent(mode: str | None = None, portals: list[str] | None = None, repeat_minutes: int | None = None,
                    headless: bool | None = None) -> int:
    setup_logging("agent")
    init_db()
    lock = AgentLock()
    if not lock.acquire():
        log.error("Another agent process is already running (see %s)", lock.path)
        return 2
    control = AgentControl()
    with session_scope() as s:
        st = AgentState.get(s)
        if agent_is_alive(st) and st.pid != os.getpid():
            log.error("Another agent (pid %s) is alive", st.pid)
            lock.release()
            return 2
        if mode:
            st.mode = mode
        if portals is not None:
            st.portals = portals
        if repeat_minutes is not None:
            st.repeat_minutes = repeat_minutes
        if st.desired_state == DesiredState.STOPPED:
            st.desired_state = DesiredState.RUNNING  # started from the command line
        st.pid = os.getpid()
        st.started_at = utcnow()
        st.heartbeat_at = utcnow()
        st.actual_state = ActualState.STARTING
        st.message = "Agent starting"
        st.last_error = ""
        st.gate_kind = None
        st.gate_response = None

    loop = asyncio.get_running_loop()

    def request_stop() -> None:
        log.info("Stop signal received")
        with contextlib.suppress(Exception):
            control.update(desired_state=DesiredState.STOPPED)

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, request_stop)

    beat = asyncio.create_task(_heartbeat(control))
    settings = get_settings()
    engine = BrowserEngine(headless=headless, checkpoint=control.checkpoint)
    assistant = JobAssistant() if settings.ai_enabled else None
    orchestrator = Orchestrator(engine, control, assistant=assistant)
    exit_code = 0
    try:
        while True:
            summary = await orchestrator.run()
            if summary["status"] == "ERROR":
                exit_code = 1
            snap = control.snapshot(fresh=True)
            with session_scope() as s:
                repeat = AgentState.get(s).repeat_minutes or 0
            if snap.desired != DesiredState.RUNNING or repeat <= 0 or summary["status"] == "STOPPED":
                break
            control.set_actual(ActualState.RUNNING, activity=f"Waiting {repeat} min before the next run")
            await engine.stop()
            try:
                end = loop.time() + repeat * 60
                while loop.time() < end:
                    await control.checkpoint()
                    await asyncio.sleep(2)
            except Exception:
                break
    except Exception as exc:
        log.exception("Agent crashed")
        control.update(last_error=f"{exc.__class__.__name__}: {exc}"[:2000], actual_state=ActualState.ERROR)
        exit_code = 1
    finally:
        beat.cancel()
        with contextlib.suppress(Exception):
            await engine.stop()
        with session_scope() as s:
            st = AgentState.get(s)
            st.actual_state = ActualState.STOPPED if st.actual_state != ActualState.ERROR else ActualState.ERROR
            st.desired_state = DesiredState.STOPPED
            st.current_activity = ""
            st.gate_kind = None
            st.pending_job_id = None
            st.pid = None
        lock.release()
        log.info("Agent stopped")
    return exit_code


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Personal Job Assistant browser agent")
    p.add_argument("--mode", choices=[m.value for m in AgentMode], help="dry_run | review | auto")
    p.add_argument("--portals", help="comma separated: naukri,linkedin,instahyre (default: profile.json)")
    p.add_argument("--repeat-minutes", type=int, help="run again every N minutes (0 = single run)")
    p.add_argument("--headless", action="store_true", help="run the browser headless (no login possible)")
    a = p.parse_args(argv)
    portals = [x.strip() for x in a.portals.split(",") if x.strip()] if a.portals else None
    return asyncio.run(run_agent(a.mode, portals, a.repeat_minutes, True if a.headless else None))


if __name__ == "__main__":
    sys.exit(main())
