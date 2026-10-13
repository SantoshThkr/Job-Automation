"""API for the live dashboard: current job/step, answering questions in the dashboard, run history and
automatic re-queueing of parked jobs once their questions are answered."""

from __future__ import annotations

import asyncio
import os
import time

from fastapi.testclient import TestClient

from app.db import session_scope
from app.engine import control as control_mod
from app.engine.context import GateResult
from app.engine.control import AgentControl, DBHumanGate
from app.main import create_app
from app.models import AgentState, Application, Job, JobStatus, QuestionAnswer, Run, utcnow


def _alive_agent(**kw) -> None:
    with session_scope() as s:
        st = AgentState.get(s)
        st.pid = os.getpid()
        st.heartbeat_at = utcnow()
        st.actual_state = "RUNNING"
        st.desired_state = "RUNNING"
        for k, v in kw.items():
            setattr(st, k, v)


def test_live_state_and_question_gate(clean_db):
    with session_scope() as s:
        job = Job(portal="linkedin", external_id="x1", url="https://example.test/j", title="React Developer",
                  company="Acme", status=JobStatus.APPLYING, match_score=91)
        s.add(job)
        s.flush()
        job_id = job.id
    payload = {"questions": [{"id": "pja-7", "question": "Do you hold a valid passport?", "type": "radio",
                              "options": ["Yes", "No"], "required": True, "suggested": ""}]}
    _alive_agent(current_job_id=job_id, current_step="Waiting for you: 1 question", current_url="https://acme.test/apply",
                 phase="applying", gate_kind="questions", gate_reason="1 question needs your answer",
                 gate_payload=payload, pending_job_id=job_id)
    with TestClient(create_app()) as c:
        st = c.get("/api/agent").json()
        assert st["current_job"]["title"] == "React Developer" and st["current_step"].startswith("Waiting")
        assert st["gate_payload"]["questions"][0]["options"] == ["Yes", "No"]
        bad = c.post("/api/agent/gate", json={"response": "approve", "answers": {"nope": "x"}})
        assert bad.status_code == 400
        ok = c.post("/api/agent/gate", json={"response": "approve", "job_id": job_id, "answers": {"pja-7": " No "}})
        assert ok.status_code == 200
    with session_scope() as s:
        st = AgentState.get(s)
        assert st.gate_response == "approve" and st.gate_answers == {"pja-7": "No"}
        st.pid = None


def test_runs_endpoint(clean_db):
    with session_scope() as s:
        s.add(Run(mode="dry_run", portals=["naukri"], status="COMPLETED", stats={"discovered": 12}))
    with TestClient(create_app()) as c:
        runs = c.get("/api/agent/runs").json()
        assert runs[0]["status"] == "COMPLETED" and runs[0]["stats"]["discovered"] == 12


def test_answering_pending_questions_requeues_parked_job(clean_db):
    with session_scope() as s:
        job = Job(portal="naukri", external_id="p1", url="u", title="UI Engineer", company="Beta",
                  status=JobStatus.MANUAL_ACTION_REQUIRED)
        s.add(job)
        s.flush()
        app = Application(job_id=job.id, status=JobStatus.MANUAL_ACTION_REQUIRED, submit_attempted=False)
        s.add(app)
        s.flush()
        s.add(QuestionAnswer(application_id=app.id, question="Do you hold a valid passport?",
                             normalized_question="do you hold a valid passport", required=True, needs_review=True))
        job_id = job.id
    with TestClient(create_app()) as c:
        r = c.post("/api/answers/memory", json={"question": "Do you hold a valid passport?", "answer": "Yes"}).json()
        assert r["requeued_jobs"] == [job_id]
        assert c.get(f"/api/jobs/{job_id}").json()["status"] == "QUEUED"
        stats = c.get("/api/stats").json()
        assert {"expired", "needs_you", "review_pending"} <= set(stats)


async def test_gate_waits_briefly_once_you_seem_away(clean_db, monkeypatch):
    """After two unanswered requests in a run (you are away), later ones wait only briefly; an answer resets it."""
    monkeypatch.setattr(control_mod, "AWAY_WAIT_S", 0.3)
    with session_scope() as s:
        AgentState.get(s).desired_state = "RUNNING"
    ctl = AgentControl(cache_s=0)
    gate = DBHumanGate(ctl, poll_s=0.05)
    assert await gate.wait("questions", "q1", timeout_s=0.2) == GateResult.TIMEOUT
    assert await gate.wait("account", "sign in", timeout_s=0.2) == GateResult.TIMEOUT
    t = time.monotonic()
    assert await gate.wait("captcha", "solve", timeout_s=60) == GateResult.TIMEOUT
    assert time.monotonic() - t < 3  # did not wait the full minute

    async def answer_soon():
        await asyncio.sleep(0.2)
        with session_scope() as s:
            AgentState.get(s).gate_response = "approve"

    task = asyncio.create_task(answer_soon())
    assert await gate.wait("manual", "help", timeout_s=5) == GateResult.RESOLVED
    await task
    assert ctl.unanswered_streak == 0
    with session_scope() as s:
        AgentState.get(s).desired_state = "STOPPED"
