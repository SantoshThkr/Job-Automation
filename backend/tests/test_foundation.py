"""Phase 1: configuration, database, profile, resumes and API foundation."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.db import session_scope
from app.engine.recorder import fingerprint, upsert_job
from app.models import AgentState, Job, JobStatus
from app.portals.types import JobPosting
from app.profile import Profile, ProfileStore, blocking_issues, validate_profile
from app.resumes import list_resumes, select_resume


@pytest.fixture
def client(clean_db):
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


def test_profile_loads_and_is_complete(profile):
    assert profile.personal.full_name == "Asha Verma"
    assert profile.professional.total_experience_years == 7
    assert blocking_issues(profile) == []


def test_example_profile_is_flagged_as_incomplete():
    from app.config import PROJECT_ROOT

    raw = json.loads((PROJECT_ROOT / "config" / "profile.example.json").read_text())
    issues = validate_profile(Profile.model_validate(raw))
    fields = {i.field for i in issues if i.severity == "error"}
    assert "profile_confirmed" in fields
    assert "personal.first_name" in fields and "personal.phone" in fields


def test_profile_store_hot_reload_and_bad_json(tmp_path, test_env):
    path = tmp_path / "p.json"
    path.write_text((test_env / "profile.json").read_text())
    store = ProfileStore(path)
    assert store.load().personal.first_name == "Asha"
    raw = json.loads(path.read_text())
    raw["personal"]["first_name"] = "Changed"
    import os
    import time

    path.write_text(json.dumps(raw))
    os.utime(path, (time.time() + 5, time.time() + 5))
    assert store.load().personal.first_name == "Changed"
    path.write_text("{ not json")
    os.utime(path, (time.time() + 10, time.time() + 10))
    # invalid file keeps the last good profile and reports the error
    assert store.load().personal.first_name == "Changed"
    assert "Could not load" in store.last_error


def test_resume_selection(profile):
    infos = list_resumes(profile)
    assert all(r.exists for r in infos)
    assert select_resume(profile, "frontend", ["React"]).id == "frontend"
    assert select_resume(profile, "fullstack", ["Node.js"]).id == "fullstack"
    assert select_resume(profile, "backend", []).id == "fullstack" or select_resume(profile, "backend", []).default


def test_job_upsert_dedupes_by_portal_id(clean_db):
    p = JobPosting(portal="naukri", external_id="123", url="https://x/1", title="React Developer", company="Acme")
    with session_scope() as s:
        job, created = upsert_job(s, p)
        assert created
        p.description = "Long description " * 10
        job2, created2 = upsert_job(s, p)
        assert not created2 and job2.id == job.id
        assert job2.description.startswith("Long description")
        assert job2.fingerprint == fingerprint("Acme", "React Developer")


def test_fingerprint_normalises_company_and_title():
    assert fingerprint("Acme Technologies Pvt. Ltd.", "React Developer (Urgent)") == \
        fingerprint("ACME", "react developer")


def test_api_endpoints(client):
    assert client.get("/api/health").json() == {"ok": True}
    prof = client.get("/api/profile").json()
    assert prof["ready_for_submission"] is True
    assert client.get("/api/stats").json()["jobs_discovered"] == 0
    agent = client.get("/api/agent").json()
    assert agent["desired_state"] == "STOPPED"
    assert len(client.get("/api/resumes").json()) == 2


def test_api_job_actions(client):
    with session_scope() as s:
        job, _ = upsert_job(s, JobPosting(portal="linkedin", external_id="9", url="u", title="UI Engineer",
                                          company="Beta"))
        job.status = JobStatus.REVIEW_PENDING
        job_id = job.id
    r = client.post(f"/api/jobs/{job_id}/action", json={"action": "approve"})
    assert r.status_code == 200 and r.json()["status"] == "QUEUED"
    r = client.post(f"/api/jobs/{job_id}/action", json={"action": "mark_applied", "note": "Applied on phone"})
    assert r.json()["status"] == "APPLIED"
    assert client.get("/api/stats").json()["today_applications"] == 1
    assert client.post(f"/api/jobs/{job_id}/action", json={"action": "approve"}).status_code == 409
    listing = client.get("/api/jobs", params={"status": "APPLIED"}).json()
    assert listing["total"] == 1 and listing["items"][0]["company"] == "Beta"


def test_api_agent_controls(client):
    r = client.post("/api/agent/start", json={"mode": "review", "portals": ["naukri"]})
    assert r.status_code == 200
    body = r.json()
    assert body["desired_state"] == "RUNNING" and body["mode"] == "review" and body["portals"] == ["naukri"]
    assert client.post("/api/agent/pause").json()["desired_state"] == "PAUSED"
    assert client.post("/api/agent/resume").json()["desired_state"] == "RUNNING"
    assert client.put("/api/agent/mode", json={"mode": "auto"}).json()["mode"] == "auto"
    assert client.put("/api/agent/mode", json={"mode": "yolo"}).status_code == 400
    assert client.post("/api/agent/gate", json={"response": "approve"}).status_code == 409  # nothing waiting
    assert client.post("/api/agent/stop").json()["desired_state"] == "STOPPED"
    with session_scope() as s:
        assert AgentState.get(s).desired_state == "STOPPED"


def test_api_evaluate_endpoint(client):
    r = client.post("/api/jobs/evaluate", json={
        "title": "Senior React Developer", "location": "Bengaluru", "experience_text": "5-8 Yrs",
        "description": "Requirements:\n- 5+ years with React, TypeScript, Redux, HTML, CSS\n- Jest"})
    assert r.status_code == 200
    assert r.json()["decision"] == "APPLY"


def test_api_answer_memory(client):
    r = client.post("/api/answers/memory", json={"question": "Are you open to night shifts?", "answer": "No"})
    assert r.status_code == 200
    mem = client.get("/api/answers/memory").json()
    assert mem[0]["answer"] == "No"
    assert client.delete(f"/api/answers/memory/{mem[0]['id']}").json() == {"ok": True}


def test_restart_persistence(clean_db):
    """Data survives an engine reset (simulates restarting the app)."""
    from app.db import reset_engine

    with session_scope() as s:
        upsert_job(s, JobPosting(portal="naukri", external_id="persist-1", url="u", title="React Dev"))
    reset_engine()
    with session_scope() as s:
        assert s.query(Job).filter_by(external_id="persist-1").count() == 1
