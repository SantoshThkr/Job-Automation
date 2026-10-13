"""LinkedIn Easy Apply on the 2026 markup (tests/fixtures/sites/linkedin-new/jobs/view/4465524746), modelled on the
live page of job #236 (Clearwater Analytics): data-view-name hooks, no <h1>, an "About the job" section, and a native
<dialog data-testid="dialog"> without role="dialog" whose Next button is data-view-name="continue-unify".

Also the regression tests for the two questions that stopped run #8 ("Job Title", "Work Authorization").
"""

from __future__ import annotations

import time

import pytest
from sqlalchemy import select

from app.ai.job_assistant import JobAssistant
from app.answers.answerer import Answerer
from app.answers.question_bank import QuestionContext
from app.browser.engine import BrowserEngine
from app.db import session_scope
from app.engine.control import AgentControl
from app.engine.orchestrator import Orchestrator
from app.engine.recorder import set_status, upsert_job
from app.matching.analyzer import JobInput
from app.models import AgentMode, AgentState, Job, JobStatus
from app.portals.linkedin import LinkedInAdapter
from app.portals.types import ApplyOutcome, JobPosting
from tests.conftest import fake_ollama
from tests.helpers import FakeGate, answers_by_question, make_ctx

JOB_ID = "4465524746"


def _posting(base: str, ext: str = JOB_ID, query: str = "") -> JobPosting:
    return JobPosting(portal="linkedin", external_id=ext, url=f"{base}/jobs/view/{ext}/{query}",
                      title="Sr. Software Development Engineer", company="Clearwater Analytics",
                      location="Noida, Uttar Pradesh, India", easy_apply=True)


def _ai_says_not_a_question() -> JobAssistant:
    """What the local model answered for "Job Title" in run #8 - for every call."""
    return JobAssistant(fake_ollama([{"category": "NOT_A_QUESTION", "confidence": 0.9}] * 40))


def _citizen(profile):
    profile.work_preferences.citizenship = ["India"]
    return profile


# ------------------------------------------------------------------ the answer engine (no browser)

async def _answer(profile, question, field_type="text", options=(), assistant=None):
    ctx = QuestionContext(question=question, field_type=field_type, options=list(options), required=True,
                          job=JobInput(title="Frontend Developer", company="Profferit", location="Noida"), portal="site")
    return await Answerer(profile, assistant).answer(ctx)


@pytest.mark.parametrize("question", ["Job Title", "Job Title*", "Current Job Title", "Job title (current)",
                                      "Designation", "What is your current designation?"])
async def test_job_title_comes_from_the_profile_even_if_the_local_ai_calls_it_a_message(profile, question):
    a = await _answer(profile, question, assistant=_ai_says_not_a_question())
    assert a.value == profile.professional.current_title == "Senior Frontend Engineer"
    assert a.mode == "profile" and a.category == "PROFILE_FACT" and not a.needs_review
    assert "professional.current_title" in a.sources


async def test_ai_not_a_question_verdict_never_blanks_a_required_field(profile):
    a = await _answer(profile, "Preferred team name", assistant=_ai_says_not_a_question())
    assert a.mode != "not_a_question"


async def test_job_49_work_authorization_with_only_a_citizen_option_is_asked(profile):
    """Run #8, job 49: options ['Select', 'Citizen']. Authorized to work in India does not mean citizen."""
    a = await _answer(profile, "Work Authorization", "select", ["Select", "Citizen"], assistant=_ai_says_not_a_question())
    assert a.value is None and a.mode == "ask" and a.needs_review and a.category == "VISA"
    assert "citizenship" in a.note and "Citizen" in a.note


async def test_work_authorization_status_from_verified_citizenship(profile):
    a = await _answer(_citizen(profile), "Work Authorization", "select",
                      ["Select an option", "Citizen", "Non-citizen", "Permanent Resident", "Work Visa"])
    assert a.value == "Citizen" and a.mode == "profile" and "work_preferences.citizenship" in a.sources


@pytest.mark.parametrize("question,ftype,options,expected", [
    ("Work Authorization", "select", ["Select", "Yes", "No"], "Yes"),
    ("Are you legally authorized to work in India?", "radio", ["Yes", "No"], "Yes"),
    ("Are you legally authorized to work in the United States?", "radio", ["Yes", "No"], "No"),
    ("Work authorization status", "select", ["Authorized to work in India", "Require sponsorship"],
     "Authorized to work in India"),
])
async def test_work_authorization_answers_from_authorized_countries(profile, question, ftype, options, expected):
    a = await _answer(profile, question, ftype, options)
    assert a.value == expected and a.category == "VISA"


async def test_citizenship_is_never_inferred_from_work_authorization(profile):
    a = await _answer(profile, "Citizenship", "select", ["India", "United States"])
    assert a.value is None and a.mode == "ask"
    b = await _answer(_citizen(profile), "Citizenship", "select", ["Indian", "American"])
    assert b.value == "Indian"


# ------------------------------------------------------------------ the 2026 job page + Easy Apply dialog

pytestmark_browser = pytest.mark.browser


@pytest.fixture
async def engine(site_server, tmp_path):
    e = BrowserEngine(headless=True, user_data_dir=tmp_path / "p", screenshot_dir=tmp_path / "s", human_delays=False)
    await e.start()
    yield e
    await e.stop()


@pytest.fixture
def li(engine, site_server):
    return LinkedInAdapter(engine, f"{site_server}/linkedin-new")


@pytestmark_browser
async def test_job_page_details_from_the_new_layout(li, site_server):
    started = time.monotonic()
    job = await li.fetch_details(_posting(li.base_url))
    assert time.monotonic() - started < 8  # no 15 s wait for selectors that no longer exist
    assert job.title == "Sr. Software Development Engineer" and job.company == "Clearwater Analytics"
    assert job.description.startswith("Clearwater Analytics is hiring")
    assert "React, TypeScript and Next.js" in job.description and "About the job" not in job.description
    assert job.easy_apply and job.apply_type == "portal"


@pytestmark_browser
async def test_description_text_is_not_a_confirmation(li, engine):
    await engine.goto(_posting(li.base_url).url)
    assert "your application was sent" in (await engine.page.inner_text("main")).lower()
    assert not await li._success(engine.page) and not await li._success(engine.page, after_submit=True)


@pytestmark_browser
async def test_easy_apply_native_dialog_end_to_end(li, engine, profile):
    """Apply -> native <dialog> -> contact -> resume -> questions -> review -> submit -> LinkedIn's confirmation."""
    job = await li.fetch_details(_posting(li.base_url))
    gate = FakeGate()
    ctx = make_ctx(engine, _citizen(profile), job, AgentMode.AUTO, gate, assistant=_ai_says_not_a_question())
    res = await li.apply(job, ctx)
    assert res.outcome == ApplyOutcome.APPLIED, (res.message, res.unresolved)
    assert res.confirmation_text == "Your application was sent to Clearwater Analytics!"
    assert gate.calls == [] and ctx.submits == [True]  # nothing needed you; exactly one submit
    sent = await engine.page.evaluate("() => window.SUBMITTED_DATA")
    assert sent["jt"] == "Senior Frontend Engineer"  # Job Title <- professional.current_title
    assert sent["wa"] == "Citizen"  # Work Authorization <- work_preferences.citizenship
    assert sent["au"] == "Yes" and sent["yr"] == "6"
    assert sent["ph"] == "9812345678" and sent["cc"] == "India (+91)" and sent["ctry"] == "India"
    assert sent["fn"] == "Asha" and sent["city"]
    assert sent["follow-company-checkbox"] is False  # never auto-follow the company
    answers = {q.question: a for q, a in ctx.answers_log}
    title = next(a for q, a in answers.items() if q.startswith("Job Title"))
    assert title.mode == "profile" and title.category == "PROFILE_FACT"


@pytestmark_browser
async def test_easy_apply_asks_only_for_unverifiable_work_authorization(li, engine, profile):
    job = await li.fetch_details(_posting(li.base_url))
    gate = FakeGate([answers_by_question({"Work Authorization": "Citizen"})])
    ctx = make_ctx(engine, profile, job, AgentMode.AUTO, gate, auto_wait=30)  # you are at the dashboard
    res = await li.apply(job, ctx)
    assert res.outcome == ApplyOutcome.APPLIED, (res.message, res.unresolved)
    asked = [q["question"] for q in gate.payloads[0]["questions"]]
    assert len(asked) == 1 and asked[0].startswith("Work Authorization")  # Job Title was not asked
    sent = await engine.page.evaluate("() => window.SUBMITTED_DATA")
    assert sent["wa"] == "Citizen" and sent["jt"] == "Senior Frontend Engineer"
    assert any(q.startswith("Work Authorization") and a == "Citizen" for q, a in ctx.remembered)


@pytestmark_browser
async def test_easy_apply_without_your_answer_is_never_submitted(li, engine, profile):
    job = await li.fetch_details(_posting(li.base_url))
    ctx = make_ctx(engine, profile, job, AgentMode.AUTO, FakeGate())  # nobody answers
    res = await li.apply(job, ctx)
    assert res.outcome == ApplyOutcome.MANUAL_ACTION_REQUIRED and "Work Authorization" in res.message
    assert "Job Title" not in res.message
    assert await engine.page.evaluate("() => window.SUBMITTED") is None
    assert await engine.page.evaluate("() => window.DISCARDED") is True  # draft discarded


@pytestmark_browser
async def test_easy_apply_dry_run_reaches_review_and_discards(li, engine, profile):
    job = await li.fetch_details(_posting(li.base_url))
    ctx = make_ctx(engine, _citizen(profile), job, AgentMode.DRY_RUN)
    res = await li.apply(job, ctx)
    assert res.outcome == ApplyOutcome.DRY_RUN and "discarded before submit" in res.message
    assert ctx.submits == [] and await engine.page.evaluate("() => window.SUBMITTED") is None
    assert await engine.page.evaluate("() => window.DISCARDED") is True


@pytestmark_browser
async def test_submit_without_linkedin_confirmation_is_not_applied(li, engine, profile):
    job = await li.fetch_details(_posting(li.base_url, query="?noconfirm=1"))
    ctx = make_ctx(engine, _citizen(profile), job, AgentMode.AUTO)
    res = await li.apply(job, ctx)
    assert await engine.page.evaluate("() => window.SUBMITTED") is True
    assert res.outcome == ApplyOutcome.MANUAL_ACTION_REQUIRED and res.submit_attempted
    assert "confirmation not verified" in res.message.lower() and res.verification == "uncertain"


@pytestmark_browser
async def test_external_apply_in_a_new_tab_hands_over_to_the_ats(li, engine, profile):
    job = await li.fetch_details(_posting(li.base_url, "4465524747"))
    assert job.apply_type == "external" and not job.easy_apply
    res = await li.apply(job, make_ctx(engine, profile, job, AgentMode.AUTO))
    assert res.outcome == ApplyOutcome.EXTERNAL and "/ats/lever/job.html" in res.external_url
    assert res.external_page is not engine.page
    await res.external_page.close()


@pytestmark_browser
async def test_external_apply_in_the_same_tab_hands_over_to_the_ats(li, engine, profile):
    job = await li.fetch_details(_posting(li.base_url, "4465524748"))
    res = await li.apply(job, make_ctx(engine, profile, job, AgentMode.AUTO))
    assert res.outcome == ApplyOutcome.EXTERNAL and "/ats/lever/job.html" in res.external_url
    assert res.external_page is engine.page


# ------------------------------------------------------------------ job #236 through the engine and database

def _seed_job_236(site_server) -> int:
    """The state run #8 left job 236 in: QUEUED after 'evaluated APPLY (score 71.1)', no application, empty JD."""
    with session_scope() as s:
        job, _ = upsert_job(s, _posting(f"{site_server}/linkedin-new"))
        job.match_score, job.decision, job.apply_type = 71.1, "APPLY", "portal"
        set_status(s, job, JobStatus.QUEUED, "Relevant (score 71.1)")
        st = AgentState.get(s)
        st.mode, st.portals, st.desired_state = "auto", ["linkedin"], "RUNNING"
        return job.id


async def _run_linkedin(engine, site_server):
    def make(name, eng):
        adapter = LinkedInAdapter(eng, f"{site_server}/linkedin-new")

        async def no_new_listings(_q):  # this test is about the queued job, not search
            return
            yield

        adapter.search = no_new_listings  # type: ignore[method-assign]
        return adapter

    await engine.start()
    await engine.context.add_cookies([{"name": "li_at", "value": "AQEDAS-test", "url": site_server}])
    control = AgentControl(cache_s=0)
    engine._checkpoint = control.checkpoint
    with session_scope() as s:
        AgentState.get(s).desired_state = "RUNNING"
    return await Orchestrator(engine, control, adapter_factory=make).run()


@pytestmark_browser
async def test_job_236_end_to_end_status_only_applied_after_confirmation(site_server, tmp_path, clean_db):
    from fastapi.testclient import TestClient

    from app.main import create_app

    engine = BrowserEngine(headless=True, user_data_dir=tmp_path / "p", screenshot_dir=tmp_path / "s",
                           human_delays=False)
    try:
        job_id = _seed_job_236(site_server)
        await _run_linkedin(engine, site_server)  # nobody at the dashboard
        with session_scope() as s:
            job = s.get(Job, job_id)
            assert job.status == JobStatus.MANUAL_ACTION_REQUIRED  # never APPLIED without a submission
            assert "Work Authorization" in job.status_reason and "Job Title" not in job.status_reason
            title = next(q for q in job.application.answers if q.question.startswith("Job Title"))
            assert title.answer == "Senior Frontend Engineer" and title.source == "profile" and not title.needs_review
        with TestClient(create_app()) as c:  # you answer once in the dashboard, then Retry
            pending = c.get("/api/questions/pending").json()
            assert [p["question"] for p in pending if p["job_id"] == job_id] == ["Work Authorization"]
            assert c.post("/api/answers/memory", json={"question": "Work Authorization", "answer": "Citizen"}).status_code == 200
            assert c.post(f"/api/jobs/{job_id}/action", json={"action": "retry"}).json()["status"] == "QUEUED"
        await _run_linkedin(engine, site_server)
        with session_scope() as s:
            job = s.scalar(select(Job).where(Job.id == job_id))
            assert job.status == JobStatus.APPLIED, job.status_reason
            assert job.application.confirmation_text == "Your application was sent to Clearwater Analytics!"
            wa = next(q for q in job.application.answers if q.question.startswith("Work Authorization"))
            assert wa.answer == "Citizen" and wa.source == "memory"
    finally:
        await engine.stop()
