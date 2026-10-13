"""End-to-end Job Engine tests: orchestrator + DB control channel + real browser + mock portals/ATS.

Covers Phase 4-7 flows end to end and Phase 10 hardening: duplicates, already-applied, failures,
unknown questions, crash recovery, stop/pause, interrupted state recovery, limits, login expiry.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.browser.engine import BrowserEngine
from app.db import session_scope
from app.engine.control import AgentControl
from app.engine.orchestrator import Orchestrator
from app.models import AgentState, Application, ApplicationEvent, Job, JobStatus, QuestionAnswer, Run
from app.portals.registry import PORTALS

pytestmark = pytest.mark.browser


def _set_state(**kw) -> None:
    with session_scope() as s:
        st = AgentState.get(s)
        for k, v in kw.items():
            setattr(st, k, v)


def _jobs() -> dict[tuple[str, str], Job]:
    with session_scope() as s:
        return {(j.portal, j.external_id): j for j in s.scalars(select(Job)).all()}


def _status(portal: str, ext: str) -> str:
    j = _jobs().get((portal, ext))
    return j.status if j else "MISSING"


@pytest.fixture
async def engine(site_server, tmp_path):
    e = BrowserEngine(headless=True, user_data_dir=tmp_path / "p", screenshot_dir=tmp_path / "s", human_delays=False)
    yield e
    await e.stop()


def factory(site_server, wrap=None):
    def make(name, engine):
        cls = PORTALS.get(name)
        if cls is None:
            return None
        adapter = cls(engine, f"{site_server}/{name}")
        return wrap(adapter) if wrap else adapter

    return make


async def _run(engine, site_server, mode, portals, *, wrap=None, assistant=None, during=None):
    _set_state(mode=mode, portals=portals, desired_state="RUNNING")
    control = AgentControl(cache_s=0)
    engine._checkpoint = control.checkpoint
    orch = Orchestrator(engine, control, assistant=assistant, adapter_factory=factory(site_server, wrap))
    if during is None:
        return await orch.run()
    task = asyncio.create_task(during())
    try:
        return await orch.run()
    finally:
        task.cancel()


async def test_dry_run_across_all_portals(engine, site_server, clean_db):
    summary = await _run(engine, site_server, "dry_run", ["naukri", "linkedin", "instahyre"])
    assert summary["status"] == "COMPLETED", summary
    expected = {
        ("naukri", "10012000000"): "DRY_RUN", ("naukri", "10022000000"): "DRY_RUN",
        ("naukri", "10032000000"): "DRY_RUN",  # external Greenhouse, filled but not submitted
        ("naukri", "10042000000"): "ALREADY_APPLIED", ("naukri", "10052000000"): "SKIPPED",
        ("naukri", "10062000000"): "DRY_RUN", ("naukri", "10072000000"): "SKIPPED",
        ("naukri", "10082000000"): "DRY_RUN",
        ("linkedin", "3001"): "DRY_RUN", ("linkedin", "3002"): "DRY_RUN", ("linkedin", "3003"): "ALREADY_APPLIED",
        ("linkedin", "3004"): "SKIPPED", ("linkedin", "3005"): "DRY_RUN", ("linkedin", "3006"): "DRY_RUN",
        # both are relevant; jobs are applied best match first, so the React role (5002) goes before 5001 and
        # max_applications_per_run (10) is reached before 5001: it stays queued for the next run
        ("instahyre", "5001"): "QUEUED", ("instahyre", "5002"): "DRY_RUN", ("instahyre", "5003"): "ALREADY_APPLIED",
        ("instahyre", "5004"): "SKIPPED",
    }
    jobs = _jobs()
    got = {k: jobs[k].status if k in jobs else "MISSING" for k in expected}
    assert got == expected
    with session_scope() as s:
        apps = s.scalars(select(Application)).all()
        assert apps and not any(a.submit_attempted or a.confirmed for a in apps)
        ext = s.scalar(select(Job).where(Job.external_id == "10032000000"))
        assert ext.application.apply_target == "external" and ext.application.ats_platform == "greenhouse"
        # the second search query finds the same Naukri jobs again: they are not processed twice
        assert summary["stats"]["seen_before"] >= 8
        skipped = s.scalar(select(Job).where(Job.external_id == "10052000000"))
        assert skipped.match_details["decision"] == "SKIP" and skipped.status_reason


async def test_auto_apply_end_to_end(engine, site_server, clean_db):
    summary = await _run(engine, site_server, "auto", ["naukri", "linkedin"])
    assert summary["status"] == "COMPLETED", summary
    expected = {
        ("naukri", "10012000000"): "APPLIED", ("naukri", "10022000000"): "APPLIED",
        ("naukri", "10032000000"): "APPLIED", ("naukri", "10042000000"): "ALREADY_APPLIED",
        ("naukri", "10052000000"): "SKIPPED", ("naukri", "10062000000"): "MANUAL_ACTION_REQUIRED",
        ("naukri", "10072000000"): "SKIPPED", ("naukri", "10082000000"): "APPLIED",
        ("linkedin", "3001"): "APPLIED", ("linkedin", "3002"): "APPLIED", ("linkedin", "3003"): "ALREADY_APPLIED",
        ("linkedin", "3004"): "SKIPPED", ("linkedin", "3005"): "MANUAL_ACTION_REQUIRED",
        ("linkedin", "3006"): "DUPLICATE",  # same company + title already applied via Naukri
    }
    jobs = _jobs()
    assert {k: jobs[k].status for k in expected} == expected
    with session_scope() as s:
        j = s.scalar(select(Job).where(Job.external_id == "10012000000"))
        app = j.application
        assert app.confirmed and app.applied_at and "successfully applied" in app.confirmation_text
        assert app.resume_id == "frontend"
        qas = s.scalars(select(QuestionAnswer).where(QuestionAnswer.application_id == app.id)).all()
        assert {q.answer for q in qas} >= {"6", "Yes"}
        types = [e.type for e in s.scalars(select(ApplicationEvent).where(ApplicationEvent.job_id == j.id))]
        assert {"evaluated", "apply_started", "submit", "result_applied"} <= set(types)
        lever = s.scalar(select(Job).where(Job.external_id == "3002"))
        assert lever.application.ats_platform == "lever" and lever.application.confirmed
        manual = s.scalar(select(Job).where(Job.external_id == "10062000000"))
        assert "blood group" in manual.application.manual_action_reason
        # (the screening chat stopped on a question and the job still offers "Apply": nothing was sent)
        assert not manual.application.submit_attempted and not manual.application.confirmed
        pending = s.scalars(select(QuestionAnswer).where(QuestionAnswer.needs_review.is_(True))).all()
        assert any("blood group" in q.question for q in pending)
        run = s.get(Run, summary["run_id"])
        assert run.status == "COMPLETED" and run.stats["result_applied"] == 6


async def test_review_mode_with_dashboard_approvals(engine, site_server, clean_db):
    decisions = ["approve", "reject"]

    async def user():
        while decisions:
            with session_scope() as s:
                st = AgentState.get(s)
                if st.gate_kind == "review" and st.gate_response is None:
                    assert st.pending_job_id is not None
                    st.gate_response = decisions.pop(0)
            await asyncio.sleep(0.2)

    summary = await _run(engine, site_server, "review", ["instahyre"], during=user)
    assert summary["status"] == "COMPLETED"
    # best match first: the React role (5002) is presented for approval before 5001
    assert _status("instahyre", "5002") == "APPLIED"
    assert _status("instahyre", "5001") == "SKIPPED"
    with session_scope() as s:
        app = s.scalar(select(Job).where(Job.external_id == "5002")).application
        assert app.review_decision == "approve" and app.confirmed


async def test_one_failing_job_does_not_stop_the_run(engine, site_server, clean_db):
    def wrap(adapter):
        original = adapter.fetch_details

        async def broken(job):
            if job.external_id == "10022000000":
                raise RuntimeError("simulated page crash while reading job")
            return await original(job)

        adapter.fetch_details = broken
        return adapter

    summary = await _run(engine, site_server, "dry_run", ["naukri"], wrap=wrap)
    assert summary["status"] == "COMPLETED"
    assert _status("naukri", "10022000000") == "FAILED"
    assert _status("naukri", "10012000000") == "DRY_RUN" and _status("naukri", "10082000000") == "DRY_RUN"
    with session_scope() as s:
        failed = s.scalar(select(Job).where(Job.external_id == "10022000000"))
        assert "simulated page crash" in failed.status_reason
        assert any(e.level == "error" for e in failed.events)


async def test_browser_crash_mid_run_recovers(engine, site_server, clean_db):
    def wrap(adapter):
        original = adapter.fetch_details

        async def crash_then_continue(job):
            if job.external_id == "10022000000" and engine.restarts == 0:
                await engine.context.close()  # the browser dies
            return await original(job)

        adapter.fetch_details = crash_then_continue
        return adapter

    summary = await _run(engine, site_server, "dry_run", ["naukri"], wrap=wrap)
    assert summary["status"] == "COMPLETED"
    assert engine.restarts == 1
    assert _status("naukri", "10022000000") == "DRY_RUN"
    assert _status("naukri", "10082000000") == "DRY_RUN"


async def test_stop_request_ends_run_cleanly(engine, site_server, clean_db):
    async def stopper():
        while True:
            if _status("naukri", "10012000000") == "DRY_RUN":
                _set_state(desired_state="STOPPED")
                return
            await asyncio.sleep(0.1)

    summary = await _run(engine, site_server, "dry_run", ["naukri"], during=stopper)
    assert summary["status"] == "STOPPED"
    assert _status("naukri", "10082000000") in {"MISSING", "NEW", "QUEUED"}  # never reached
    with session_scope() as s:
        assert not s.scalars(select(Job).where(Job.status == JobStatus.APPLYING)).all()


async def test_pause_and_resume(engine, site_server, clean_db):
    seen = []

    async def pauser():
        while _status("naukri", "10012000000") != "DRY_RUN":
            await asyncio.sleep(0.1)
        _set_state(desired_state="PAUSED")
        for _ in range(50):
            with session_scope() as s:
                if AgentState.get(s).actual_state == "PAUSED":
                    seen.append("paused")
                    break
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.5)
        _set_state(desired_state="RUNNING")

    summary = await _run(engine, site_server, "dry_run", ["naukri"], during=pauser)
    assert summary["status"] == "COMPLETED" and seen == ["paused"]
    assert _status("naukri", "10082000000") == "DRY_RUN"


async def test_recovers_interrupted_applications(engine, site_server, clean_db):
    with session_scope() as s:
        safe = Job(portal="instahyre", external_id="5002", url=f"{site_server}/instahyre/job-5002-react-js-developer-at-orbit-learning-remote/",
                   title="React JS Developer", company="Orbit Learning", location="Remote", status=JobStatus.APPLYING,
                   description="React", fingerprint="orbit learning|react js developer")
        s.add(safe)
        s.flush()
        s.add(Application(job_id=safe.id, status=JobStatus.APPLYING, submit_attempted=False))
        risky = Job(portal="instahyre", external_id="9999", url="u", title="X", company="Y",
                    status=JobStatus.AWAITING_APPROVAL, fingerprint="y|x")
        s.add(risky)
        s.flush()
        s.add(Application(job_id=risky.id, status=JobStatus.AWAITING_APPROVAL, submit_attempted=True))
        s.add(Run(mode="auto", portals=["instahyre"], status="RUNNING"))
    summary = await _run(engine, site_server, "auto", ["instahyre"])
    assert summary["stats"]["recovered"] == 2
    assert _status("instahyre", "5002") == "APPLIED"  # re-queued and completed
    assert _status("instahyre", "9999") == "MANUAL_ACTION_REQUIRED"  # may have been submitted: verify manually
    with session_scope() as s:
        assert s.scalars(select(Run).where(Run.status == "INTERRUPTED")).first() is not None


async def test_daily_limit_defers_applications(engine, site_server, clean_db, profile, monkeypatch):
    from app.profile import get_profile_store

    limited = profile.model_copy(deep=True)
    limited.search.max_applications_per_run = 1
    monkeypatch.setattr(get_profile_store(), "load", lambda force=False: limited)
    summary = await _run(engine, site_server, "dry_run", ["instahyre"])
    assert summary["stats"]["apply_attempts"] == 1
    assert summary["stats"]["deferred"] >= 1
    assert _status("instahyre", "5002") == "DRY_RUN"  # the best match used the only slot
    assert _status("instahyre", "5001") == "QUEUED"  # kept for the next run


async def test_login_expired_skips_portal_but_continues(engine, site_server, clean_db):
    await engine.start()
    await engine.context.add_cookies([{"name": "mock_logged_out", "value": "1", "url": site_server}])
    summary = await _run(engine, site_server, "dry_run", ["naukri", "instahyre"])
    assert summary["status"] == "COMPLETED"
    assert summary["stats"]["login_required"] == 1
    assert _status("instahyre", "5001") == "DRY_RUN"
    with session_scope() as s:
        events = s.scalars(select(ApplicationEvent).where(ApplicationEvent.type == "login_required")).all()
        assert any("naukri" in e.message.lower() and "logged in" in e.message.lower() for e in events)


async def test_incomplete_profile_never_submits(engine, site_server, clean_db, monkeypatch):
    from app.engine import orchestrator as orch_mod
    from app.profile import ProfileIssue

    monkeypatch.setattr(orch_mod, "blocking_issues",
                        lambda p: [ProfileIssue(field="personal.phone", message="Phone is a placeholder", severity="error")])
    summary = await _run(engine, site_server, "auto", ["instahyre"])
    assert summary["status"] == "COMPLETED"
    # relevant jobs stay queued (nothing is filled with placeholder data); you are told once
    assert _status("instahyre", "5001") == "QUEUED" and _status("instahyre", "5002") == "QUEUED"
    assert summary["stats"]["waiting_for_profile"] >= 2
    with session_scope() as s:
        assert not s.scalars(select(Application)).all()
        ev = s.scalars(select(ApplicationEvent).where(ApplicationEvent.type == "profile_incomplete")).all()
        assert ev and "Phone is a placeholder" in ev[0].message


async def test_user_answer_then_retry_completes_application(engine, site_server, clean_db):
    """Unknown question -> MANUAL; you answer it once in the dashboard; Retry -> applied using your answer."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    summary = await _run(engine, site_server, "auto", ["naukri"])
    assert _status("naukri", "10062000000") == "MANUAL_ACTION_REQUIRED"
    with TestClient(create_app()) as c:
        pending = c.get("/api/questions/pending").json()
        q = next(p for p in pending if "blood group" in p["question"])
        job_id = q["job_id"]
        saved = c.post("/api/answers/memory", json={"question": q["question"], "answer": "O+"})
        assert saved.status_code == 200
        # nothing had been sent (the chat stopped; Naukri still offered "Apply"), so answering the one question that
        # blocked it puts the job back in the queue by itself - no Retry, no force
        assert job_id in saved.json()["requeued_jobs"]
        assert c.get(f"/api/jobs/{job_id}").json()["status"] == "QUEUED"
        assert all("blood group" not in p["question"] for p in c.get("/api/questions/pending").json())
    summary = await _run(engine, site_server, "auto", ["naukri"])
    assert summary["stats"]["result_applied"] >= 1
    assert _status("naukri", "10062000000") == "APPLIED"
    with session_scope() as s:
        job = s.scalar(select(Job).where(Job.external_id == "10062000000"))
        qa = next(q for q in job.application.answers if "blood group" in q.question)
        assert qa.answer == "O+" and qa.source == "memory" and not qa.needs_review
