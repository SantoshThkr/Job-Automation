"""Phase 5: external ATS detection and applications (Greenhouse, Lever, Workday, SmartRecruiters, unknown)."""

from __future__ import annotations

import pytest

from app.ai.job_assistant import JobAssistant
from app.ats.external import apply_external
from app.ats.registry import detect_ats
from app.browser.engine import BrowserEngine
from app.engine.context import GateResult
from app.models import AgentMode
from app.portals.types import ApplyOutcome, JobPosting
from tests.conftest import fake_ollama
from tests.helpers import FakeGate, make_ctx

pytestmark = pytest.mark.browser

JOB = JobPosting(portal="naukri", external_id="ext-1", url="https://example.test/job", title="UI Engineer - React",
                 company="Gamma Health", location="Bengaluru",
                 description="Build React and TypeScript web apps. 5+ years of experience.")


@pytest.fixture
async def engine(site_server, tmp_path):
    e = BrowserEngine(headless=True, user_data_dir=tmp_path / "p", screenshot_dir=tmp_path / "s", human_delays=False)
    await e.start()
    yield e
    await e.stop()


async def _open(engine, url):
    await engine.goto(url)
    return engine.page


@pytest.mark.parametrize("path,expected", [
    ("/ats/greenhouse/job.html?gh_jid=1003", "greenhouse"),
    ("/ats/lever/job.html", "lever"),
    ("/ats/workday/job.html", "workday"),
    ("/ats/smartrecruiters/job.html", "smartrecruiters"),
    ("/ats/unknown/apply.html", None),
])
async def test_detect_ats(engine, site_server, path, expected):
    page = await _open(engine, site_server + path)
    cls = await detect_ats(page)
    assert (cls.name if cls else None) == expected


def test_detect_by_real_urls():
    from app.ats.adapters import GreenhouseAdapter, LeverAdapter, SmartRecruitersAdapter, WorkdayAdapter

    assert GreenhouseAdapter.matches_url("https://boards.greenhouse.io/acme/jobs/4012345")
    assert GreenhouseAdapter.matches_url("https://job-boards.greenhouse.io/acme/jobs/4012345")
    assert LeverAdapter.matches_url("https://jobs.lever.co/acme/1b2c3d4e-aaaa/apply")
    assert WorkdayAdapter.matches_url("https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/Bengaluru/Frontend_R123")
    assert SmartRecruitersAdapter.matches_url("https://jobs.smartrecruiters.com/Acme/743999912345678-frontend-dev")
    assert not LeverAdapter.matches_url("https://careers.acme.com/jobs/123")


async def test_greenhouse_auto_apply(engine, site_server, profile):
    page = await _open(engine, f"{site_server}/ats/greenhouse/job.html?gh_jid=1003")
    ctx = make_ctx(engine, profile, JOB, AgentMode.AUTO)
    res = await apply_external(engine, page, ctx)
    assert res.outcome == ApplyOutcome.APPLIED, res.message
    assert res.ats == "greenhouse" and "Thank you" in res.confirmation_text
    answers = {q.question: a.value for q, a in ctx.answers_log}
    assert answers["LinkedIn Profile"] == profile.personal.linkedin_url
    assert answers["Are you legally authorized to work in India?"] == "Yes"
    assert answers["What is your notice period (in days)?"] == "30"
    assert answers["Gender"] == "Female"


async def test_greenhouse_dry_run_fills_but_does_not_submit(engine, site_server, profile):
    page = await _open(engine, f"{site_server}/ats/greenhouse/job.html?gh_jid=1003")
    ctx = make_ctx(engine, profile, JOB, AgentMode.DRY_RUN)
    res = await apply_external(engine, page, ctx)
    assert res.outcome == ApplyOutcome.DRY_RUN
    assert ctx.submits == []
    assert await page.locator("#first_name").input_value() == "Asha"
    assert "confirmation" not in page.url


async def test_greenhouse_required_motivation_without_ai_is_manual(engine, site_server, profile):
    page = await _open(engine, f"{site_server}/ats/greenhouse/job-why.html")
    ctx = make_ctx(engine, profile, JOB, AgentMode.AUTO)
    res = await apply_external(engine, page, ctx)
    assert res.outcome == ApplyOutcome.MANUAL_ACTION_REQUIRED
    assert "Why do you want to work" in res.message and ctx.submits == []


async def test_greenhouse_required_motivation_with_ai(engine, site_server, profile):
    text = ("I build fast, accessible React and TypeScript interfaces, and Gamma Health's patient-facing web apps "
            "are exactly the kind of product I want to work on.")
    ctx = make_ctx(engine, profile, JOB, AgentMode.AUTO,
                   assistant=JobAssistant(fake_ollama([{"answer": text, "confidence": 0.9}])))
    page = await _open(engine, f"{site_server}/ats/greenhouse/job-why.html")
    res = await apply_external(engine, page, ctx)
    assert res.outcome == ApplyOutcome.APPLIED, res.message
    ai = [a for q, a in ctx.answers_log if a.source == "ai"]
    assert ai and ai[0].value == text and 0.75 <= ai[0].confidence < 1


async def test_lever_apply(engine, site_server, profile):
    page = await _open(engine, f"{site_server}/ats/lever/job.html")
    ctx = make_ctx(engine, profile, JOB, AgentMode.AUTO)
    res = await apply_external(engine, page, ctx)
    assert res.outcome == ApplyOutcome.APPLIED, res.message
    assert res.ats == "lever"
    answers = {q.question: a.value for q, a in ctx.answers_log}
    assert answers.get("How many years of experience do you have with TypeScript?") == "4-5"


async def test_lever_captcha_requires_human(engine, site_server, profile):
    page = await _open(engine, f"{site_server}/ats/lever/apply-captcha.html")
    ctx = make_ctx(engine, profile, JOB, AgentMode.AUTO)
    res = await apply_external(engine, page, ctx)
    assert res.outcome == ApplyOutcome.MANUAL_ACTION_REQUIRED
    assert "captcha" in res.message.lower() and res.submit_attempted


async def test_lever_captcha_solved_by_user_in_review_mode(engine, site_server, profile):
    async def user_solves(page):
        await page.evaluate("""() => { document.body.innerHTML = '<h3>Application submitted!</h3>'; }""")
        return GateResult.RESOLVED

    gate = FakeGate([GateResult.APPROVE, user_solves])
    page = await _open(engine, f"{site_server}/ats/lever/apply-captcha.html")
    res = await apply_external(engine, page, make_ctx(engine, profile, JOB, AgentMode.REVIEW, gate))
    assert res.outcome == ApplyOutcome.APPLIED, res.message
    assert [k for k, _ in gate.calls] == ["review", "captcha"]


async def test_workday_multistep(engine, site_server, profile):
    page = await _open(engine, f"{site_server}/ats/workday/job.html")
    ctx = make_ctx(engine, profile, JOB, AgentMode.AUTO)
    res = await apply_external(engine, page, ctx)
    assert res.outcome == ApplyOutcome.APPLIED, res.message
    assert res.ats == "workday" and "Application Submitted" in res.confirmation_text
    answers = {q.question: a.value for q, a in ctx.answers_log}
    assert answers["How Did You Hear About Us?*"] == "Job Board"
    assert answers["Are you legally authorized to work in India?*"] == "Yes"


async def test_workday_dry_run_walks_steps_without_submitting(engine, site_server, profile):
    page = await _open(engine, f"{site_server}/ats/workday/job.html")
    ctx = make_ctx(engine, profile, JOB, AgentMode.DRY_RUN)
    res = await apply_external(engine, page, ctx)
    assert res.outcome == ApplyOutcome.DRY_RUN, res.message
    assert await engine.page.locator("#s4").is_visible()  # reached review step
    assert not await engine.page.locator("#done").is_visible()


async def test_workday_sign_in_wall(engine, site_server, profile):
    page = await _open(engine, f"{site_server}/ats/workday/job-signin.html")
    res = await apply_external(engine, page, make_ctx(engine, profile, JOB, AgentMode.AUTO))
    assert res.outcome == ApplyOutcome.MANUAL_ACTION_REQUIRED and "sign" in res.message.lower()


async def test_smartrecruiters_shadow_dom_two_steps(engine, site_server, profile):
    page = await _open(engine, f"{site_server}/ats/smartrecruiters/job.html")
    ctx = make_ctx(engine, profile, JOB, AgentMode.AUTO)
    res = await apply_external(engine, page, ctx)
    assert res.outcome == ApplyOutcome.APPLIED, res.message
    assert res.ats == "smartrecruiters"


async def test_unknown_ats_is_manual_never_applied(engine, site_server, profile):
    page = await _open(engine, f"{site_server}/ats/unknown/apply.html")
    ctx = make_ctx(engine, profile, JOB, AgentMode.AUTO)
    res = await apply_external(engine, page, ctx)
    assert res.outcome == ApplyOutcome.MANUAL_ACTION_REQUIRED
    assert res.ats == "unknown" and ctx.submits == []


async def test_unexpected_navigation_after_submit_is_not_applied(engine, site_server, profile):
    page = await _open(engine, f"{site_server}/ats/greenhouse/job-badsubmit.html?gh_jid=77")
    ctx = make_ctx(engine, profile, JOB, AgentMode.AUTO)
    res = await apply_external(engine, page, ctx)
    assert res.outcome == ApplyOutcome.MANUAL_ACTION_REQUIRED
    assert res.submit_attempted and "no confirmation" in res.message.lower()


async def test_otp_after_submit_requires_human(engine, site_server, profile):
    page = await _open(engine, f"{site_server}/ats/lever/apply-otp.html")
    res = await apply_external(engine, page, make_ctx(engine, profile, JOB, AgentMode.AUTO))
    assert res.outcome == ApplyOutcome.MANUAL_ACTION_REQUIRED and "otp" in res.message.lower()
