"""Cross-portal regression tests for the central application engine.

The same questions (tests/fixtures/sites/kit/kit.js) are asked on a company career site (navigator), a known ATS,
LinkedIn Easy Apply (2026 dialog), Instahyre's apply modal and Naukri's screening chat. On every portal:

* known profile facts, derived facts, your saved answers and grounded written answers are filled automatically,
* the local AI calling a required field "not a question" never blanks it,
* dropdowns, card-style radios, a custom listbox and generic placeholder labels are read correctly,
* a value the site rejects is re-entered in the format it expects (same fact),
* only what is genuinely unknown or legal (blood group, citizenship status) is asked - once - and the open
  application continues from the same page with your answer,
* a job is APPLIED only after the site confirms.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.ai.job_assistant import JobAssistant
from app.ats.external import apply_external
from app.browser.engine import BrowserEngine
from app.db import session_scope
from app.engine.recorder import memory_lookup_fn, remember_answer
from app.models import AgentMode, AnswerMemory
from app.portals.types import ApplyOutcome, JobPosting
from tests.conftest import fake_ollama
from tests.helpers import FakeGate, answers_by_question, make_ctx

pytestmark = pytest.mark.browser

GENUINELY_UNKNOWN = {"Work Authorization": "Citizen", "blood group": "O+"}
EXPECTED = {  # what the kit form must contain when it is submitted (profile: tests/fixtures/profile_test.json)
    "k_email": "asha.verma.dev@gmail.com",  # profile fact
    "k_phone": "9812345678",  # profile fact, re-entered in the 10-digit format the site wants
    "k_months": "0",  # derived: 7 years -> 0 additional months
    "k_loc": "Yes",  # derived: current city Bengaluru
    "k_fresher": "No",  # derived from 7 years of experience (card-style radio without labels)
    "k_title": "Senior Frontend Engineer",  # profile fact, although the local AI says "not a question"
    "k_au": "Yes",  # authorized_countries = India
    "k_wa": "Citizen",  # not in the profile: asked once
    "k_np_v": "30 days",  # custom listbox <- notice period 30 days
    "k_gql": "2",  # your saved answer, given once before in other words
    "k_blood": "O+",  # not in the profile: asked once
    "k_gh": "https://github.com/ashaverma",  # "Enter URL here" box under the "GitHub profile" heading
    "k_d1": "Built SPAs in React and Redux. Technologies: React, Redux, JavaScript.",  # that role's work history
}


def ai_says_not_a_question() -> JobAssistant:
    return JobAssistant(fake_ollama([{"category": "NOT_A_QUESTION", "confidence": 0.9}] * 60))


@pytest.fixture
async def engine(site_server, tmp_path):
    e = BrowserEngine(headless=True, user_data_dir=tmp_path / "p", screenshot_dir=tmp_path / "s", human_delays=False)
    await e.start()
    yield e
    await e.stop()


@pytest.fixture
def memory(clean_db):
    """Your earlier answer to 'GraphQL experience (years)' - a different wording of the kit's GraphQL question."""
    with session_scope() as s:
        remember_answer(s, "GraphQL experience (years)", "2")
    return memory_lookup_fn()


def kit_ctx(engine, profile, job, gate, memory, *, mode=AgentMode.AUTO, auto_wait=30):
    ctx = make_ctx(engine, profile, job, mode, gate, assistant=ai_says_not_a_question(), auto_wait=auto_wait)
    ctx.answerer.memory_lookup = memory

    async def remember(question, answer):  # your dashboard answers are saved like in a real run
        ctx.remembered.append((question, answer))
        with session_scope() as s:
            remember_answer(s, question, answer, company=job.company, portal=job.portal)

    ctx.remember = remember
    return ctx


def asked(gate: FakeGate) -> list[str]:
    return [q["question"] for p in gate.payloads if p for q in p.get("questions", [])]


def check_kit(sent: dict, *, why_company: str, skip: tuple[str, ...] = ()) -> None:
    for key, want in EXPECTED.items():
        if key in skip or key not in sent:
            continue
        assert sent[key] == want, (key, sent[key])
    if "k_why" in sent:
        assert why_company in sent["k_why"] and len(sent["k_why"]) > 40
    assert sent.get("k_deco") in (None, False)  # the label-less widget checkbox was left alone


# ---------------------------------------------------------------- company career site (navigator)

def _site_job(base: str, query: str = "") -> JobPosting:
    return JobPosting(portal="naukri", external_id="kit-1", url=f"{base}/careers/kit/job.html{query}",
                      title="Frontend Engineer", company="KitCo", location="Bengaluru",
                      description="React TypeScript Next.js Redux Jest. 5+ years of frontend experience.")


async def test_company_site_kit_only_genuine_questions_are_asked(engine, site_server, profile, memory):
    job = _site_job(site_server)
    gate = FakeGate([answers_by_question(GENUINELY_UNKNOWN)])
    ctx = kit_ctx(engine, profile, job, gate, memory)
    await engine.goto(job.url)
    res = await apply_external(engine, engine.page, ctx)
    page = engine.page
    assert res.outcome == ApplyOutcome.APPLIED, (res.message, res.unresolved, ctx.events[-6:])
    assert "Thank you for applying" in res.confirmation_text
    assert sorted(asked(gate)) == ["What is your blood group?", "Work Authorization"]
    assert await page.evaluate("() => window.SEARCHED") is None  # the job-search filters were never submitted
    assert await page.evaluate("() => window.CONTACTED") is None  # nor the sales contact form
    assert await page.evaluate("() => sessionStorage.kitLoads") == "1"  # your answers went into the same open form
    check_kit(await page.evaluate("() => window.SUBMITTED"), why_company="KitCo")
    assert any(t == "form_repair" for t, _ in ctx.events)  # the site rejected '+91...', 10 digits were re-entered
    assert len(ctx.submits) == 2, [e for e in ctx.events if e[0] in {"step", "form_errors", "form_repair", "submit"}]


async def test_company_site_without_confirmation_is_never_applied(engine, site_server, profile, memory):
    job = _site_job(site_server)
    gate = FakeGate([answers_by_question(GENUINELY_UNKNOWN)])
    ctx = kit_ctx(engine, profile, job, gate, memory)
    await engine.goto(job.url)
    await engine.page.goto(f"{site_server}/careers/kit/apply.html?noconfirm=1")
    res = await apply_external(engine, engine.page, ctx)
    assert await engine.page.evaluate("() => !!window.SUBMITTED")
    assert res.outcome == ApplyOutcome.MANUAL_ACTION_REQUIRED and res.submit_attempted
    assert "confirmation not verified" in res.message.lower() and res.verification == "uncertain"


async def test_nobody_answers_means_needs_you_with_only_the_genuine_questions(engine, site_server, profile, memory):
    job = _site_job(site_server)
    ctx = kit_ctx(engine, profile, job, FakeGate(), memory, auto_wait=0)
    await engine.goto(job.url)
    res = await apply_external(engine, engine.page, ctx)
    assert res.outcome == ApplyOutcome.MANUAL_ACTION_REQUIRED and not res.submit_attempted
    assert set(res.unresolved) == {"Work Authorization", "What is your blood group?"}, res.unresolved
    assert await engine.page.evaluate("() => window.SUBMITTED") is None


# ---------------------------------------------------------------- known ATS (Greenhouse)

async def test_greenhouse_kit(engine, site_server, profile, memory):
    job = JobPosting(portal="naukri", external_id="kit-gh", url=f"{site_server}/ats/greenhouse/job-kit.html?gh_jid=9",
                     title="Frontend Engineer", company="KitCo", location="Bengaluru", description="React TypeScript")
    gate = FakeGate([answers_by_question(GENUINELY_UNKNOWN)])
    ctx = kit_ctx(engine, profile, job, gate, memory)
    await engine.goto(job.url)
    res = await apply_external(engine, engine.page, ctx)
    assert res.outcome == ApplyOutcome.APPLIED and res.ats == "greenhouse", (res.message, res.unresolved)
    assert sorted(asked(gate)) == ["What is your blood group?", "Work Authorization"]
    import json

    sent = json.loads(await engine.page.evaluate("() => sessionStorage.kitSent"))
    check_kit(sent, why_company="KitCo")
    assert any(t == "form_repair" for t, _ in ctx.events)


# ---------------------------------------------------------------- LinkedIn Easy Apply (2026 dialog)

async def test_linkedin_easy_apply_kit(engine, site_server, profile, memory):
    from app.portals.linkedin import LinkedInAdapter

    li = LinkedInAdapter(engine, f"{site_server}/linkedin-new")
    job = await li.fetch_details(JobPosting(portal="linkedin", external_id="4465524749",
                                            url=f"{site_server}/linkedin-new/jobs/view/4465524749/",
                                            title="Frontend Engineer", company="KitCo"))
    gate = FakeGate([answers_by_question(GENUINELY_UNKNOWN)])
    ctx = kit_ctx(engine, profile, job, gate, memory)
    res = await li.apply(job, ctx)
    assert res.outcome == ApplyOutcome.APPLIED, (res.message, res.unresolved, ctx.events[-5:])
    assert res.confirmation_text == "Your application was sent to KitCo!"
    assert sorted(asked(gate)) == ["What is your blood group?", "Work Authorization"]
    assert not any(t == "handoff" for t, _ in ctx.events)  # the adapter itself handled every step
    sent = await engine.page.evaluate("() => window.SUBMITTED_DATA")
    check_kit(sent, why_company="KitCo")
    assert sent["follow-company-checkbox"] is False


# ---------------------------------------------------------------- Instahyre apply modal

async def test_instahyre_modal_kit(engine, site_server, profile, memory):
    from app.portals.instahyre import InstahyreAdapter

    ih = InstahyreAdapter(engine, f"{site_server}/instahyre")
    job = JobPosting(portal="instahyre", external_id="5005", title="Frontend Engineer", company="KitCo",
                     url=f"{site_server}/instahyre/job-5005-frontend-engineer-at-kitco-bangalore/", location="Bangalore")
    gate = FakeGate([answers_by_question(GENUINELY_UNKNOWN)])
    ctx = kit_ctx(engine, profile, job, gate, memory)
    res = await ih.apply(job, ctx)
    assert res.outcome == ApplyOutcome.APPLIED, (res.message, res.unresolved)
    assert sorted(asked(gate)) == ["What is your blood group?", "Work Authorization"]  # same flow as other portals
    check_kit(await engine.page.evaluate("() => window.SUBMITTED"), why_company="KitCo")


# ---------------------------------------------------------------- Naukri screening chat

async def test_naukri_chat_kit(engine, site_server, profile, memory):
    from app.portals.naukri import NaukriAdapter

    nk = NaukriAdapter(engine, f"{site_server}/naukri")
    job = JobPosting(portal="naukri", external_id="10152000000", title="Frontend Engineer", company="KitCo",
                     url=f"{site_server}/naukri/job-listings-frontend-engineer-kitco-bengaluru-10152000000",
                     location="Bengaluru")
    gate = FakeGate([answers_by_question({"blood group": "O+"})])
    ctx = kit_ctx(engine, profile, job, gate, memory)
    res = await nk.apply(job, ctx)
    assert res.outcome == ApplyOutcome.APPLIED, (res.message, ctx.events[-5:])
    assert asked(gate) == ["What is your blood group?"]
    # Job Title, authorized, fresher, months, blood group, GraphQL (saved), located in Bengaluru
    assert await engine.page.evaluate("() => window.ANSWERS") == [
        "Senior Frontend Engineer", "Yes", "No", "0", "O+", "2", "Yes"]


# ---------------------------------------------------------------- one answer, every portal

async def test_your_answers_are_reused_on_the_next_portal(engine, site_server, profile, memory):
    """Answered once on the company site; LinkedIn then asks nothing - the open questions are known now."""
    from app.portals.linkedin import LinkedInAdapter

    job = _site_job(site_server)
    ctx = kit_ctx(engine, profile, job, FakeGate([answers_by_question(GENUINELY_UNKNOWN)]), memory)
    await engine.goto(job.url)
    assert (await apply_external(engine, engine.page, ctx)).outcome == ApplyOutcome.APPLIED
    with session_scope() as s:
        every = list(s.scalars(select(AnswerMemory)))
        saved = {m.question: (m.answer, m.scope) for m in every if m.scope != "category"}
        keys = {m.normalized_question: m.answer for m in every if m.scope == "category"}
    assert saved["What is your blood group?"] == ("O+", "global")
    assert keys["fact: blood group"] == "O+"  # ...and under a wording-independent key ("Blood group" asks the same)
    assert saved["Work Authorization"] == ("Citizen", "global")

    li = LinkedInAdapter(engine, f"{site_server}/linkedin-new")
    li_job = await li.fetch_details(JobPosting(portal="linkedin", external_id="4465524749", title="Frontend Engineer",
                                               url=f"{site_server}/linkedin-new/jobs/view/4465524749/", company="KitCo"))
    gate = FakeGate()  # nobody at the dashboard: anything asked would park the job
    res = await li.apply(li_job, kit_ctx(engine, profile, li_job, gate, memory_lookup_fn()))
    assert res.outcome == ApplyOutcome.APPLIED and gate.calls == [], (res.message, res.unresolved)


# ---------------------------------------------------------------- what the site says after submitting

async def test_success_message_in_the_error_box_is_a_confirmation(engine, site_server, profile, memory):
    job = _site_job(site_server)
    ctx = kit_ctx(engine, profile, job, FakeGate([answers_by_question(GENUINELY_UNKNOWN)]), memory)
    await engine.goto(f"{site_server}/careers/kit/apply.html?successbox=1")
    res = await apply_external(engine, engine.page, ctx)
    assert res.outcome == ApplyOutcome.APPLIED and "sent successfully" in res.confirmation_text, res.message


async def test_server_error_after_submit_is_explained_not_applied(engine, site_server, profile, memory):
    job = _site_job(site_server)
    ctx = kit_ctx(engine, profile, job, FakeGate([answers_by_question(GENUINELY_UNKNOWN)]), memory)
    await engine.goto(f"{site_server}/careers/kit/apply.html?servererror=1")
    res = await apply_external(engine, engine.page, ctx)
    assert res.outcome == ApplyOutcome.MANUAL_ACTION_REQUIRED and res.submit_attempted
    assert "returned an error page ('502 Bad Gateway')" in res.message


# ---------------------------------------------------------------- portal state the runs misread

async def test_naukri_lost_apply_click_is_clicked_again(engine, site_server, profile, memory):
    from app.portals.naukri import NaukriAdapter

    nk = NaukriAdapter(engine, f"{site_server}/naukri")
    job = JobPosting(portal="naukri", external_id="10172000000", title="React Developer", company="Lazy Pages",
                     url=f"{site_server}/naukri/job-listings-react-developer-lazy-pages-bengaluru-10172000000")
    ctx = kit_ctx(engine, profile, job, FakeGate(), memory)
    res = await nk.apply(job, ctx)
    assert res.outcome == ApplyOutcome.APPLIED, res.message
    assert await engine.page.evaluate("() => window.CLICKS") == 2
    assert any(t == "apply_retry" for t, _ in ctx.events)


async def test_instahyre_application_sent_earlier_is_already_applied(engine, site_server, profile, memory):
    from app.portals.instahyre import InstahyreAdapter

    ih = InstahyreAdapter(engine, f"{site_server}/instahyre")
    job = JobPosting(portal="instahyre", external_id="5006", title="React Developer", company="Bloom Value",
                     url=f"{site_server}/instahyre/job-5006-react-developer-at-bloom-value-bangalore/")
    res = await ih.apply(job, kit_ctx(engine, profile, job, FakeGate(), memory))
    assert res.outcome == ApplyOutcome.ALREADY_APPLIED, res.message


async def test_stuck_ats_hands_over_on_its_own_tab(engine, site_server, profile, memory, monkeypatch):
    """Run 5/8: the SmartRecruiters adapter got stuck on the company tab and the navigator continued on the Naukri
    tab (where the local AI clicked 'Become a Pro'). The navigator must continue where the adapter stopped."""
    import app.ats.external as external
    import app.navigator.agent as agent
    from app.portals.types import ApplyResult

    portal_tab = engine.page
    await portal_tab.goto(f"{site_server}/naukri/job-listings-ui-engineer-react-gamma-health-bengaluru-10032000000")
    company_tab = await engine.context.new_page()
    await company_tab.goto(f"{site_server}/careers/kit/job.html")

    class StuckAdapter:
        name, display_name = "smartrecruiters", "SmartRecruiters"

        def __init__(self, engine):
            self.engine = engine

        async def apply(self, page, ctx):
            self.engine.use_page(page)
            return ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED, "Could not find the next/submit button",
                               handoff=True)

    async def fake_detect(_page):
        return StuckAdapter

    seen = {}

    async def fake_navigate(engine_, page, ctx, **kw):
        seen["url"] = page.url
        return ApplyResult(ApplyOutcome.DRY_RUN, "stop here")

    monkeypatch.setattr(external, "detect_ats", fake_detect)
    monkeypatch.setattr(agent, "navigate_application", fake_navigate)
    job = _site_job(site_server)
    await apply_external(engine, company_tab, kit_ctx(engine, profile, job, FakeGate(), memory))
    assert seen["url"].endswith("/careers/kit/job.html")  # not the Naukri tab


async def test_known_ats_never_submits_a_page_without_its_form(engine, site_server, profile, memory, monkeypatch):
    """Run 8, job 137: 'Greenhouse' detected on an SSO page, 0 fields filled, Submit clicked. Never again."""
    import app.ats.external as external
    from app.ats.adapters import GreenhouseAdapter

    async def misdetect(_page):
        return GreenhouseAdapter

    monkeypatch.setattr(external, "detect_ats", misdetect)
    await engine.goto(f"{site_server}/linkedin-new/login/")  # a sign-in page with a Submit-like button
    job = _site_job(site_server)
    ctx = kit_ctx(engine, profile, job, FakeGate(), memory, auto_wait=0)
    res = await apply_external(engine, engine.page, ctx)
    assert ctx.submits == [] and res.outcome != ApplyOutcome.APPLIED


# ---------------------------------------------------------------- engine + database: park, answer, continue

async def _run_naukri(engine, site_server):
    from app.engine.control import AgentControl
    from app.engine.orchestrator import Orchestrator
    from app.models import AgentState
    from app.portals.naukri import NaukriAdapter

    def make(name, eng):
        adapter = NaukriAdapter(eng, f"{site_server}/naukri")

        async def no_new_listings(_q):  # the test is about the queued job
            return
            yield

        adapter.search = no_new_listings  # type: ignore[method-assign]
        return adapter

    with session_scope() as s:
        st = AgentState.get(s)
        st.mode, st.portals, st.desired_state = "auto", ["naukri"], "RUNNING"
    control = AgentControl(cache_s=0)
    engine._checkpoint = control.checkpoint
    return await Orchestrator(engine, control, adapter_factory=make).run()


async def test_parked_application_continues_after_you_answer(site_server, tmp_path, memory):
    from fastapi.testclient import TestClient

    from app.engine.recorder import set_status, upsert_job
    from app.main import create_app
    from app.models import Job, JobStatus

    engine = BrowserEngine(headless=True, user_data_dir=tmp_path / "p", screenshot_dir=tmp_path / "s",
                           human_delays=False)
    try:
        with session_scope() as s:
            job, _ = upsert_job(s, JobPosting(
                portal="naukri", external_id="10162000000", title="Frontend Engineer", company="KitCo",
                url=f"{site_server}/naukri/job-listings-frontend-engineer-kitco-bengaluru-10162000000",
                location="Bengaluru", description="React TypeScript Next.js"))
            job.match_score, job.decision = 90.0, "APPLY"
            set_status(s, job, JobStatus.QUEUED, "Relevant")
            job_id = job.id
        await _run_naukri(engine, site_server)  # nobody at the dashboard
        with session_scope() as s:
            job = s.get(Job, job_id)
            assert job.status == JobStatus.MANUAL_ACTION_REQUIRED and not job.application.submit_attempted
        with TestClient(create_app()) as c:
            item = next(i for i in c.get("/api/needs-you").json()["items"] if i["job_id"] == job_id)
            assert item["stop_kind"] == "questions" and item["ats"].startswith("site:")
            assert sorted(q["question"] for q in item["questions"]) == ["What is your blood group?", "Work Authorization"]
            assert item["resume"] == "continue_url"  # Retry goes back to the company site where it stopped
            r = c.post(f"/api/needs-you/{job_id}/answers", json={"answers": [
                {"question": "What is your blood group?", "answer": "O+"},
                {"question": "Work Authorization", "answer": "Citizen"}]}).json()
            assert r["requeued"]
        await _run_naukri(engine, site_server)
        with session_scope() as s:
            job = s.get(Job, job_id)
            assert job.status == JobStatus.APPLIED, job.status_reason
            assert "Thank you for applying" in job.application.confirmation_text
            sources = {q.question: q.source for q in job.application.answers if q.question in
                       {"What is your blood group?", "Work Authorization", "Job Title"}}
            assert sources["What is your blood group?"] == "memory" and sources["Work Authorization"] == "memory"
            assert sources["Job Title"] == "profile"
    finally:
        await engine.stop()
