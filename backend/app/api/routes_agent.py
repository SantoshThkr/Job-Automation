"""Agent control endpoints: start / pause / resume / stop, mode, and human-gate responses."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from datetime import date, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.serializers import agent_dict, run_dict
from app.config import PROJECT_ROOT, get_settings
from app.db import get_db
from app.engine.control import agent_is_alive
from app.engine.recorder import add_event
from app.models import ActualState, AgentMode, AgentState, DesiredState, Job, Run, utcnow

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agent", tags=["agent"])

VALID_PORTALS = {"naukri", "linkedin", "instahyre"}
Name = Annotated[str, StringConstraints(max_length=32)]  # a mode / portal name; checked against the known ones


class Strict(BaseModel):
    """Request bodies accept the documented fields only."""
    model_config = ConfigDict(extra="forbid")


def _state(db: Session) -> dict[str, Any]:
    st = AgentState.get(db)
    current = db.get(Job, st.current_job_id) if st.current_job_id else None
    return agent_dict(st, agent_is_alive(st), current)


@router.get("")
def get_agent(db: Session = Depends(get_db)) -> dict[str, Any]:
    return _state(db)


class StartRequest(Strict):
    mode: Name | None = None
    portals: Annotated[list[Name], Field(max_length=8)] | None = None
    repeat_minutes: Annotated[int, Field(ge=0, le=24 * 60)] | None = None


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
            raise HTTPException(400, "unknown portal(s); use naukri, linkedin, instahyre")
        st.portals = list(dict.fromkeys(body.portals))
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


class RecencyRequest(Strict):
    """One of: ``days`` (1, 3, 7, 14, 30, 60, 90), a custom ``start`` / ``end`` date range, or ``all`` (every date -
    only when chosen explicitly)."""
    days: Annotated[int, Field(ge=1, le=90)] | None = None
    start: date | None = None
    end: date | None = None
    all: bool = False


@router.put("/recency")
def set_recency(body: RecencyRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    from app.matching.posting import CHOICES, local_tz

    chosen = [body.days is not None, body.start is not None or body.end is not None, body.all]
    if sum(chosen) != 1:
        raise HTTPException(422, "Choose exactly one: days, a date range (start/end), or all")
    today = datetime.now(local_tz()).date()
    if body.days is not None:
        if body.days not in CHOICES:
            raise HTTPException(422, f"days must be one of {list(CHOICES)}")
        stored: dict[str, Any] = {"days": body.days}
    elif body.all:
        stored = {"all": True}
    else:
        if body.start is None:
            raise HTTPException(422, "A date range needs a start date")
        end = body.end or today
        if body.start > end:
            raise HTTPException(422, "The start date is after the end date")
        if end > today or body.start < today - timedelta(days=366):
            raise HTTPException(422, "The range must lie within the last year and not in the future")
        stored = {"start": body.start.isoformat(), "end": end.isoformat() if body.end else None}
    st = AgentState.get(db)
    st.recency = stored
    add_event(db, None, "recency", f"Job recency set to: {_label(stored)} (search, queue and the last check before "
                                   "each application use it from the next run on)")
    db.flush()
    return _state(db)


def _label(stored: dict) -> str:
    from app.matching.posting import window_from

    return window_from(stored).label()


class ModeRequest(Strict):
    mode: Name


@router.put("/mode")
def set_mode(body: ModeRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    if body.mode not in {m.value for m in AgentMode}:
        raise HTTPException(400, f"mode must be one of {[m.value for m in AgentMode]}")
    st = AgentState.get(db)
    st.mode = body.mode
    db.flush()
    return _state(db)


GateKey = Annotated[str, StringConstraints(max_length=128)]
GateAnswer = Annotated[str, StringConstraints(max_length=5000)]


class GateResponse(Strict):
    response: Name  # approve | reject
    job_id: Annotated[int, Field(ge=1)] | None = None
    # question id -> your answer (gate kind "questions")
    answers: Annotated[dict[GateKey, GateAnswer], Field(max_length=100)] | None = None


@router.post("/gate")
def gate(body: GateResponse, db: Session = Depends(get_db)) -> dict[str, Any]:
    if body.response not in {"approve", "reject"}:
        raise HTTPException(400, "response must be approve or reject")
    st = AgentState.get(db)
    if not st.gate_kind:
        raise HTTPException(409, "The agent is not waiting for you right now")
    if body.job_id is not None and st.pending_job_id is not None and body.job_id != st.pending_job_id:
        raise HTTPException(409, "The agent is waiting on a different job")
    if body.answers is not None:
        known = {str(q.get("id")) for q in (st.gate_payload or {}).get("questions", [])}
        unknown = set(body.answers) - known
        if unknown:
            raise HTTPException(400, f"Unknown question id(s): {sorted(unknown)}")
        st.gate_answers = {k: v.strip() for k, v in body.answers.items() if v and v.strip()}
    st.gate_response = body.response
    db.flush()
    return _state(db)


@router.get("/runs")
def runs(limit: int = Query(20, ge=1, le=100), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = db.scalars(select(Run).order_by(Run.id.desc()).limit(limit)).all()
    return [run_dict(r) for r in rows]


@router.get("/runs/{run_id}/breakdown")
def run_breakdown(run_id: Annotated[int, Field(ge=1, le=10_000_000)], audit: bool = False,
                  db: Session = Depends(get_db)) -> dict[str, Any]:
    """One run, reconciled: sightings vs unique listings vs new records; every decided job once - by outcome, portal,
    reason code, stage and who decided; with ``audit``, the run's skips judged again by today's rules (read-only)."""
    from app.engine.run_report import run_breakdown as breakdown
    from app.profile import get_profile_store

    out = breakdown(db, run_id, audit=audit, profile=get_profile_store().load() if audit else None)
    if out is None:
        raise HTTPException(404, "Run not found")
    return out
