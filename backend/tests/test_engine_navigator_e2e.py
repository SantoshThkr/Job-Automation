"""Orchestrator + DB control channel + navigator on company sites: the dashboard question flow, answer memory,
continuing a parked application where it stopped, live status, and priority ordering."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.browser.engine import BrowserEngine
from app.db import session_scope
from app.engine.control import AgentControl
from app.engine.orchestrator import Orchestrator, is_network_error
from app.engine.recorder import remember_answer
from app.models import AgentState, AnswerMemory, Application, ApplicationEvent, Job, JobStatus
from app.portals.registry import PORTALS

pytestmark = pytest.mark.browser


def _set_state(**kw) -> None:
    with session_scope() as s:
        st = AgentState.get(s)
        for k, v in kw.items():
            setattr(st, k, v)


@pytest.fixture
async def engine(site_server, tmp_path):
    e = BrowserEngine(headless=True, user_data_dir=tmp_path / "p", screenshot_dir=tmp_path / "s", human_delays=False)
    yield e
    await e.stop()


def factory(site_server):
    def make(name, engine):
        cls = PORTALS.get(name)
        return cls(engine, f"{site_server}/{name}") if cls else None

    return make


def _queued_company_job(site_server: str, *, resume_url: str = "", score: float = 90.0, ext: str = "77001",
                        title: str = "Senior Frontend Engineer") -> int:
    with session_scope() as s:
        job = Job(portal="naukri", external_id=ext, url=f"{site_server}/careers/acme/job.html", title=title,
                  company=f"Acme {ext}", location="Bengaluru", description="React TypeScript Next.js frontend role",
                  status=JobStatus.QUEUED, match_score=score, decision="APPLY", apply_type="external",
                  fingerprint=f"acme {ext}|{title.lower()}",
                  match_details={"category": "frontend", "resume_category": "frontend"})
        s.add(job)
        s.flush()
        if resume_url:
            s.add(Application(job_id=job.id, status=JobStatus.MANUAL_ACTION_REQUIRED, apply_target="external",
                              resume_url=resume_url, submit_attempted=False))
        return job.id


async def _run(engine, site_server, mode, portals, during=None):
    _set_state(mode=mode, portals=portals, desired_state="RUNNING")
    control = AgentControl(cache_s=0)
    engine._checkpoint = control.checkpoint
    orch = Orchestrator(engine, control, adapter_factory=factory(site_server), network_probe=lambda: True)
    if during is None:
        return await orch.run()
    task = asyncio.create_task(during())
    try:
        return await orch.run()
    finally:
        task.cancel()


async def test_parked_application_continues_on_company_site_with_saved_answer(engine, site_server, clean_db,
                                                                              monkeypatch):
    from app.profile import get_profile_store

    profile = get_profile_store().load(force=True).model_copy(deep=True)
    profile.search.queries = []  # no discovery: only the queue
    monkeypatch.setattr(get_profile_store(), "load", lambda force=False: profile)
    job_id = _queued_company_job(site_server, resume_url=f"{site_server}/careers/acme/apply.html")
    with session_scope() as s:
        remember_answer(s, "Do you hold a valid passport?", "Yes")  # answered once, earlier
    summary = await _run(engine, site_server, "auto", ["naukri"])
    assert summary["status"] == "COMPLETED", summary
    with session_scope() as s:
        job = s.get(Job, job_id)
        assert job.status == JobStatus.APPLIED, job.status_reason
        app = job.application
        assert app.confirmed and app.ats_platform.startswith("site:") and app.resume_url == ""
        passport = next(q for q in app.answers if "passport" in q.question.lower())
        assert passport.answer == "Yes" and passport.source == "memory"
        msgs = [e.message for e in s.scalars(select(ApplicationEvent).where(ApplicationEvent.job_id == job_id))]
        assert any("continuing on the company site" in m for m in msgs)
        assert app.steps >= 3


async def test_dashboard_question_flow_fills_submits_and_remembers(engine, site_server, clean_db, monkeypatch):
    from app.config import get_settings
    from app.profile import get_profile_store

    monkeypatch.setattr(get_settings(), "auto_human_wait_s", 30)
    profile = get_profile_store().load(force=True).model_copy(deep=True)
    profile.search.queries = []
    monkeypatch.setattr(get_profile_store(), "load", lambda force=False: profile)
    # an external application that was parked on the company's job page
    job_id = _queued_company_job(site_server, resume_url=f"{site_server}/careers/acme/job.html")
    seen = {}

    async def you():
        while True:
            with session_scope() as s:
                st = AgentState.get(s)
                if st.gate_kind == "questions" and st.gate_response is None:
                    qs = (st.gate_payload or {}).get("questions", [])
                    seen["questions"] = qs
                    seen["step"] = st.current_step
                    seen["job"] = st.current_job_id
                    st.gate_answers = {q["id"]: "No" for q in qs if "passport" in q["question"].lower()}
                    st.gate_response = "approve"
                    return
            await asyncio.sleep(0.2)

    summary = await _run(engine, site_server, "auto", ["naukri"], during=you)
    assert summary["status"] == "COMPLETED", summary
    assert seen["questions"] and seen["questions"][0]["options"] == ["Yes", "No"]
    assert seen["job"] == job_id and "Waiting for you" in seen["step"]
    with session_scope() as s:
        job = s.get(Job, job_id)
        assert job.status == JobStatus.APPLIED, job.status_reason
        qa = next(q for q in job.application.answers if "passport" in q.question.lower())
        assert qa.answer == "No" and qa.source == "user" and not qa.needs_review
        mem = s.scalar(select(AnswerMemory).where(AnswerMemory.normalized_question.like("%passport%")))
        assert mem is not None and mem.answer == "No"
        st = AgentState.get(s)
        assert st.gate_kind is None and st.current_job_id is None  # cleaned up after the run


async def test_queue_is_applied_best_match_first(engine, site_server, clean_db, monkeypatch):
    from app.profile import get_profile_store

    profile = get_profile_store().load(force=True).model_copy(deep=True)
    profile.search.queries = []
    profile.search.max_applications_per_run = 1
    monkeypatch.setattr(get_profile_store(), "load", lambda force=False: profile)
    low = _queued_company_job(site_server, score=70, ext="1", title="UI Developer")
    high = _queued_company_job(site_server, score=95, ext="2", title="Senior React Developer")
    await _run(engine, site_server, "dry_run", ["naukri"])
    with session_scope() as s:
        assert s.get(Job, high).status == JobStatus.DRY_RUN
        assert s.get(Job, low).status == JobStatus.QUEUED


def test_network_error_detection():
    assert is_network_error(Exception("Page.goto: net::ERR_NAME_NOT_RESOLVED at https://www.naukri.com"))
    assert not is_network_error(Exception("Timeout 30000ms exceeded"))
