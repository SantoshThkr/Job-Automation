"""The real agent process: started from the CLI and from the dashboard API (Start / Stop buttons)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.models import AgentState, Job

pytestmark = pytest.mark.browser

BACKEND = Path(__file__).resolve().parents[1]


def _env(site_server: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update({
        "PJA_INSTAHYRE_BASE_URL": f"{site_server}/instahyre",
        "PJA_NAUKRI_BASE_URL": f"{site_server}/naukri",
        "PJA_LINKEDIN_BASE_URL": f"{site_server}/linkedin",
        "PJA_BROWSER_HEADLESS": "true",
        "PJA_HUMAN_DELAY_MS_MAX": "0",
    })
    return env


def test_agent_cli_run_to_completion(site_server, clean_db):
    proc = subprocess.run([sys.executable, "-m", "app.agent", "--mode", "dry_run", "--portals", "instahyre",
                           "--headless"], cwd=BACKEND, env=_env(site_server), capture_output=True, text=True,
                          timeout=180)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    with session_scope() as s:
        statuses = {j.external_id: j.status for j in s.scalars(select(Job).where(Job.portal == "instahyre"))}
        st = AgentState.get(s)
        assert st.actual_state == "STOPPED" and st.pid is None
    assert statuses["5001"] == "DRY_RUN" and statuses["5004"] == "SKIPPED"
    assert not (get_settings().data_path / "agent.lock").exists()


def test_dashboard_start_and_stop_spawns_agent(site_server, clean_db, monkeypatch):
    from app.main import create_app

    for k, v in _env(site_server).items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(get_settings(), "agent_spawn_enabled", True)
    with TestClient(create_app()) as c:
        r = c.post("/api/agent/start", json={"mode": "review", "portals": ["instahyre"]})
        assert r.status_code == 200
        pid = r.json()["pid"]
        assert pid
        # in review mode the agent waits for our approval of the first relevant job
        deadline = time.time() + 90
        state = {}
        while time.time() < deadline:
            state = c.get("/api/agent").json()
            if state["gate_kind"] == "review":
                break
            time.sleep(0.5)
        assert state["gate_kind"] == "review", state
        assert state["alive"] and state["actual_state"] == "WAITING_REVIEW"
        job_id = state["pending_job_id"]
        assert c.get(f"/api/jobs/{job_id}").json()["status"] == "AWAITING_APPROVAL"
        assert c.post("/api/agent/gate", json={"response": "approve", "job_id": job_id}).status_code == 200
        deadline = time.time() + 60
        while time.time() < deadline and c.get(f"/api/jobs/{job_id}").json()["status"] != "APPLIED":
            time.sleep(0.5)
        assert c.get(f"/api/jobs/{job_id}").json()["status"] == "APPLIED"
        c.post("/api/agent/stop")
        deadline = time.time() + 60
        while time.time() < deadline:
            state = c.get("/api/agent").json()
            if not state["alive"] and state["actual_state"] == "STOPPED":
                break
            time.sleep(0.5)
        assert state["actual_state"] == "STOPPED" and not state["alive"]
    try:
        os.kill(pid, 0)
        time.sleep(2)
        os.kill(pid, 0)
        alive = True
    except OSError:
        alive = False
    assert not alive, "agent process should have exited"
