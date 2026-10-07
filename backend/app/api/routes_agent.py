"""Agent control endpoints: start / pause / resume / stop, mode, and human-gate responses."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.serializers import agent_dict
from app.config import PROJECT_ROOT, get_settings
from app.db import get_db
from app.engine.control import agent_is_alive
from app.engine.recorder import add_event
from app.models import ActualState, AgentMode, AgentState, DesiredState, utcnow

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agent", tags=["agent"])

VALID_PORTALS = {"naukri", "linkedin", "instahyre"}


def _state(db: Session) -> dict[str, Any]:
    st = AgentState.get(db)
    return agent_dict(st, agent_is_alive(st))


@router.get("")
def get_agent(db: Session = Depends(get_db)) -> dict[str, Any]:
    return _state(db)


class StartRequest(BaseModel):
    mode: str | None = None
    portals: list[str] | None = None
    repeat_minutes: int | None = None


def spawn_agent() -> int:
    settings = get_settings()
    settings.ensure_dirs()
    log_file = open(settings.logs_path / "agent-stdout.log", "ab")  # noqa: SIM115 - handed to the child
    proc = subprocess.Popen(  # noqa: S603 - fixed command
        [sys.executable, "-m", "app.agent"],
        cwd=str(PROJECT_ROOT / "backend"),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    # reap the child when it exits so it does not linger as a zombie process
    threading.Thread(target=proc.wait, name=f"reap-agent-{proc.pid}", daemon=True).start()
    return proc.pid


@router.post("/start")
def start(body: StartRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    st = AgentState.get(db)
    if body.mode:
        if body.mode not in {m.value for m in AgentMode}:
            raise HTTPException(400, f"mode must be one of {[m.value for m in AgentMode]}")
        st.mode = body.mode
    if body.portals is not None:
        bad = set(body.portals) - VALID_PORTALS
        if bad:
            raise HTTPException(400, f"unknown portals {sorted(bad)}")
        st.portals = body.portals
    if body.repeat_minutes is not None:
        st.repeat_minutes = max(0, body.repeat_minutes)
    st.desired_state = DesiredState.RUNNING
    alive = agent_is_alive(st)
    if not alive:
        st.actual_state = ActualState.STARTING
        st.message = "Starting agent..."
        st.last_error = ""
        st.heartbeat_at = utcnow()
        db.flush()
        db.commit()
        if get_settings().agent_spawn_enabled:
            pid = spawn_agent()
            st.pid = pid
            log.info("Spawned agent process pid=%s", pid)
    add_event(db, None, "agent_start", f"Start requested (mode={st.mode}, portals={st.portals or 'profile default'})")
    db.flush()
    return _state(db)


@router.post("/pause")
def pause(db: Session = Depends(get_db)) -> dict[str, Any]:
    st = AgentState.get(db)
    if st.desired_state == DesiredState.STOPPED:
        raise HTTPException(409, "Agent is not running")
    st.desired_state = DesiredState.PAUSED
    db.flush()
    return _state(db)


@router.post("/resume")
def resume(db: Session = Depends(get_db)) -> dict[str, Any]:
    st = AgentState.get(db)
    if st.desired_state == DesiredState.STOPPED:
        raise HTTPException(409, "Agent is not running - use start")
    st.desired_state = DesiredState.RUNNING
    db.flush()
    return _state(db)


@router.post("/stop")
def stop(db: Session = Depends(get_db)) -> dict[str, Any]:
    st = AgentState.get(db)
    st.desired_state = DesiredState.STOPPED
    if not agent_is_alive(st):
        st.actual_state = ActualState.STOPPED
        st.message = "Stopped"
    db.flush()
    return _state(db)


class ModeRequest(BaseModel):
    mode: str


@router.put("/mode")
def set_mode(body: ModeRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    if body.mode not in {m.value for m in AgentMode}:
        raise HTTPException(400, f"mode must be one of {[m.value for m in AgentMode]}")
    st = AgentState.get(db)
    st.mode = body.mode
    db.flush()
    return _state(db)


class GateResponse(BaseModel):
    response: str  # approve|reject
    job_id: int | None = None


@router.post("/gate")
def gate(body: GateResponse, db: Session = Depends(get_db)) -> dict[str, Any]:
    if body.response not in {"approve", "reject"}:
        raise HTTPException(400, "response must be approve or reject")
    st = AgentState.get(db)
    if not st.gate_kind:
        raise HTTPException(409, "The agent is not waiting for you right now")
    if body.job_id is not None and st.pending_job_id is not None and body.job_id != st.pending_job_id:
        raise HTTPException(409, "The agent is waiting on a different job")
    st.gate_response = body.response
    db.flush()
    return _state(db)
