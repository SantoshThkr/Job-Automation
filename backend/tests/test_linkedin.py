"""Phase 6: LinkedIn adapter against a local mock of LinkedIn's job search, job view and Easy Apply modal.

Live verification against linkedin.com needs your logged-in session and is not part of the automated tests.
"""

from __future__ import annotations

import pytest

from app.browser.engine import BrowserEngine
from app.browser.errors import LoginRequired
from app.engine.context import GateResult
from app.models import AgentMode
from app.portals.base import SearchQuery
from app.portals.linkedin import LinkedInAdapter
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
def linkedin(engine, site_server):
    return LinkedInAdapter(engine, f"{site_server}/linkedin")


async def _job(li, job_id):
    async for j in li.search(SearchQuery("Frontend Engineer", "Bengaluru", max_pages=2)):
        if j.external_id == job_id:
            return await li.fetch_details(j)
    raise AssertionError(job_id)


def test_search_url(linkedin):
    url = linkedin.search_url(SearchQuery("React Developer", "Bengaluru", posted_within_days=7, easy_apply_only=True), 3)
    assert "/jobs/search/?" in url and "keywords=React+Developer" in url
    assert "f_TPR=r604800" in url and "f_AL=true" in url and "start=50" in url


async def test_login_and_session_expiry(linkedin, engine, site_server):
    assert await linkedin.is_logged_in()
    await engine.context.add_cookies([{"name": "li_logged_out", "value": "1", "url": site_server}])
    assert not await linkedin.is_logged_in()
    with pytest.raises(LoginRequired):
        await linkedin.ensure_logged_in(None)

    async def user_logs_in(page):
        await page.fill("#username", "me@example.com")
        await page.fill("#password", "x")
        await page.click("#signin")
        await page.wait_for_url("**/feed/")
        return GateResult.RESOLVED

    await linkedin.ensure_logged_in(FakeGate([user_logs_in]))
    assert await linkedin.is_logged_in()


async def test_search_and_details(linkedin):
    jobs = [j async for j in linkedin.search(SearchQuery("Frontend Engineer", "Bengaluru", max_pages=2))]
    assert [j.external_id for j in jobs] == ["3001", "3002", "3003", "3004", "3005", "3006"]
    assert jobs[0].title == "Senior Frontend Engineer" and jobs[0].company == "Acme Cloud" and jobs[0].easy_apply
    assert jobs[2].already_applied
    d = await linkedin.fetch_details(jobs[0])
    assert "Requirements" in d.description and d.apply_type == "portal" and d.easy_apply
    assert d.location.startswith("Bengaluru")
    ext = await linkedin.fetch_details(jobs[1])
    assert ext.apply_type == "external"


async def test_easy_apply_multistep_auto(linkedin, engine, profile):
    job = await _job(linkedin, "3001")
    ctx = make_ctx(engine, profile, job, AgentMode.AUTO)
    res = await linkedin.apply(job, ctx)
    assert res.outcome == ApplyOutcome.APPLIED, (res.message, res.unresolved)
    assert "application was sent" in res.confirmation_text
    page = engine.page
    assert await page.evaluate("() => window.SUBMITTED") is True
    assert await page.evaluate("() => window.SELECTED_RESUME") == "resume-frontend.pdf"  # existing resume reused
    assert await page.locator("#ph").input_value() == "9812345678"
    assert await page.locator("#cc").input_value() == "India (+91)"
    assert await page.locator("#q1").input_value() == "6"
    assert not await page.locator("#follow-company-checkbox").is_checked()  # we do not auto-follow companies


async def test_easy_apply_dry_run_discards(linkedin, engine, profile):
    job = await _job(linkedin, "3001")
    ctx = make_ctx(engine, profile, job, AgentMode.DRY_RUN)
    res = await linkedin.apply(job, ctx)
    assert res.outcome == ApplyOutcome.DRY_RUN
    assert await engine.page.evaluate("() => window.SUBMITTED") is None
    assert await engine.page.evaluate("() => window.DISCARDED") is True


async def test_easy_apply_unknown_question_auto_is_manual(linkedin, engine, profile):
    job = await _job(linkedin, "3005")
    res = await linkedin.apply(job, make_ctx(engine, profile, job, AgentMode.AUTO))
    assert res.outcome == ApplyOutcome.MANUAL_ACTION_REQUIRED
    assert "design pattern" in res.message
    assert await engine.page.evaluate("() => window.DISCARDED") is True


async def test_easy_apply_review_reject(linkedin, engine, profile):
    job = await _job(linkedin, "3001")
    gate = FakeGate([GateResult.REJECT])
    res = await linkedin.apply(job, make_ctx(engine, profile, job, AgentMode.REVIEW, gate))
    assert res.outcome == ApplyOutcome.REJECTED and gate.calls[0][0] == "review"
    assert await engine.page.evaluate("() => window.SUBMITTED") is None


async def test_external_apply_hands_over_to_ats(linkedin, engine, profile):
    job = await _job(linkedin, "3002")
    res = await linkedin.apply(job, make_ctx(engine, profile, job, AgentMode.AUTO))
    assert res.outcome == ApplyOutcome.EXTERNAL and "/ats/lever/" in res.external_url
    await res.external_page.close()


async def test_already_applied(linkedin, engine, profile):
    job = await _job(linkedin, "3003")
    assert job.already_applied
    res = await linkedin.apply(job, make_ctx(engine, profile, job, AgentMode.AUTO))
    assert res.outcome == ApplyOutcome.ALREADY_APPLIED
