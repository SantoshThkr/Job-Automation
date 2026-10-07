"""Phase 4: Naukri adapter against a local mock of Naukri's markup and apply chatbot.

The mock reproduces Naukri's search-result cards (current and older markup), job detail page,
'Apply' / 'Apply on company site' / 'Applied' buttons and the screening chatbot. Live verification
against naukri.com needs your logged-in session and is not part of the automated tests.
"""

from __future__ import annotations

import pytest

from app.browser.engine import BrowserEngine
from app.engine.context import GateResult
from app.models import AgentMode
from app.portals.base import SearchQuery
from app.portals.naukri import NaukriAdapter
from app.portals.types import ApplyOutcome
from tests.helpers import FakeGate, make_ctx

pytestmark = pytest.mark.browser


@pytest.fixture
async def engine(site_server, tmp_path):
    e = BrowserEngine(headless=True, user_data_dir=tmp_path / "p", screenshot_dir=tmp_path / "s", human_delays=False)
    await e.start()
    yield e
    await e.stop()


@pytest.fixture
def naukri(engine, site_server):
    return NaukriAdapter(engine, f"{site_server}/naukri")


async def _job(naukri, job_id):
    async for j in naukri.search(SearchQuery("React Developer", "Bengaluru", max_pages=2)):
        if j.external_id == job_id:
            return await naukri.fetch_details(j)
    raise AssertionError(f"job {job_id} not found")


def test_search_url(naukri):
    url = naukri.search_url(SearchQuery("React Developer", "Bengaluru", experience_years=7, posted_within_days=7), 2)
    assert url.split("?")[0].endswith("/react-developer-jobs-in-bengaluru-2")
    assert "k=React+Developer" in url and "experience=7" in url and "jobAge=7" in url


async def test_login_detection_and_relogin(naukri, engine, site_server):
    assert await naukri.is_logged_in()
    await engine.context.add_cookies([{"name": "mock_logged_out", "value": "1", "url": site_server}])
    assert not await naukri.is_logged_in()

    async def user_logs_in(page):
        await page.fill("#usernameField", "me@example.com")
        await page.fill("#passwordField", "secret")
        await page.click("#login-btn")
        await page.wait_for_url("**/mnjuser/homepage")
        return GateResult.RESOLVED

    gate = FakeGate([user_logs_in])
    await naukri.ensure_logged_in(gate)
    assert gate.calls[0][0] == "login"
    assert await naukri.is_logged_in()


async def test_search_paginates_and_parses_both_markups(naukri):
    jobs = [j async for j in naukri.search(SearchQuery("React Developer", "Bengaluru", max_pages=3))]
    ids = [j.external_id for j in jobs]
    assert len(jobs) == 8 and len(set(ids)) == 8
    first = jobs[0]
    assert first.title == "Senior React Developer" and first.company == "Acme Fintech Solutions"
    assert first.experience_text == "5-8 Yrs" and first.location == "Bengaluru"
    assert "React.js" in first.skills
    old = next(j for j in jobs if j.external_id == "10082000000")  # page 2 uses the older markup
    assert old.title == "React Developer" and old.company == "Iota Media" and old.location == "Bengaluru"
    limited = [j async for j in naukri.search(SearchQuery("React Developer", "Bengaluru", max_pages=3, max_jobs=3))]
    assert len(limited) == 3


async def test_fetch_details(naukri):
    job = await _job(naukri, "10012000000")
    assert "Requirements" in job.description and "React" in job.description
    assert job.apply_type == "portal" and not job.already_applied
    assert job.salary_text == "25-35 Lacs P.A."
    ext = await _job(naukri, "10032000000")
    assert ext.apply_type == "external"
    applied = await _job(naukri, "10042000000")
    assert applied.already_applied


async def test_dry_run_never_clicks_apply(naukri, engine, profile):
    job = await _job(naukri, "10012000000")
    ctx = make_ctx(engine, profile, job, AgentMode.DRY_RUN)
    res = await naukri.apply(job, ctx)
    assert res.outcome == ApplyOutcome.DRY_RUN
    assert ctx.submits == []
    assert not await engine.page.locator(".chatbot_DrawerContentWrapper").is_visible()


async def test_auto_apply_answers_chatbot(naukri, engine, profile):
    job = await _job(naukri, "10012000000")
    ctx = make_ctx(engine, profile, job, AgentMode.AUTO)
    res = await naukri.apply(job, ctx)
    assert res.outcome == ApplyOutcome.APPLIED, res.message
    assert "successfully applied" in res.confirmation_text
    assert await engine.page.evaluate("() => window.ANSWERS") == ["6", "Yes"]
    questions = [q.question for q, _ in ctx.answers_log]
    assert any("React" in q for q in questions) and any("relocate" in q for q in questions)
    assert ctx.submits == [True]


async def test_one_click_apply_without_questions(naukri, engine, profile):
    job = await _job(naukri, "10022000000")
    res = await naukri.apply(job, make_ctx(engine, profile, job, AgentMode.AUTO))
    assert res.outcome == ApplyOutcome.APPLIED


async def test_unknown_question_requires_manual_action(naukri, engine, profile):
    job = await _job(naukri, "10062000000")
    res = await naukri.apply(job, make_ctx(engine, profile, job, AgentMode.AUTO))
    assert res.outcome == ApplyOutcome.MANUAL_ACTION_REQUIRED
    assert "blood group" in res.message and res.submit_attempted


async def test_review_mode_approve_and_reject(naukri, engine, profile):
    job = await _job(naukri, "10022000000")
    gate = FakeGate([GateResult.REJECT])
    res = await naukri.apply(job, make_ctx(engine, profile, job, AgentMode.REVIEW, gate))
    assert res.outcome == ApplyOutcome.REJECTED and gate.calls[0][0] == "review"
    job = await _job(naukri, "10022000000")
    res = await naukri.apply(job, make_ctx(engine, profile, job, AgentMode.REVIEW, FakeGate([GateResult.APPROVE])))
    assert res.outcome == ApplyOutcome.APPLIED


async def test_review_mode_user_answers_unknown_question_in_browser(naukri, engine, profile):
    job = await _job(naukri, "10062000000")

    async def user_answers(page):
        await page.locator(".textArea").fill("O+")
        await page.click(".sendMsg")
        await page.wait_for_selector(".apply-message")
        return GateResult.RESOLVED

    gate = FakeGate([GateResult.APPROVE, user_answers])
    res = await naukri.apply(job, make_ctx(engine, profile, job, AgentMode.REVIEW, gate))
    assert res.outcome == ApplyOutcome.APPLIED, res.message
    assert [k for k, _ in gate.calls] == ["review", "manual"]


async def test_external_redirect_and_already_applied(naukri, engine, profile):
    job = await _job(naukri, "10032000000")
    res = await naukri.apply(job, make_ctx(engine, profile, job, AgentMode.AUTO))
    assert res.outcome == ApplyOutcome.EXTERNAL
    assert "greenhouse" in res.external_url and res.external_page is not None
    await res.external_page.close()
    job = await _job(naukri, "10042000000")
    res = await naukri.apply(job, make_ctx(engine, profile, job, AgentMode.AUTO))
    assert res.outcome == ApplyOutcome.ALREADY_APPLIED


async def test_incomplete_profile_blocks_submission(naukri, engine, profile):
    job = await _job(naukri, "10022000000")
    ctx = make_ctx(engine, profile, job, AgentMode.AUTO, submit_blockers=["phone is a placeholder"])
    res = await naukri.apply(job, ctx)
    assert res.outcome == ApplyOutcome.MANUAL_ACTION_REQUIRED and ctx.submits == []
