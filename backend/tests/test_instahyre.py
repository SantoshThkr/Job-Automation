"""Phase 7: Instahyre adapter against a local mock of Instahyre's opportunities, job page and apply modal.

Live verification against instahyre.com needs your logged-in session and is not part of the automated tests.
"""

from __future__ import annotations

import pytest

from app.browser.engine import BrowserEngine
from app.engine.context import GateResult
from app.models import AgentMode
from app.portals.base import SearchQuery
from app.portals.instahyre import InstahyreAdapter
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
def ih(engine, site_server):
    return InstahyreAdapter(engine, f"{site_server}/instahyre")


async def _job(ih, job_id):
    async for j in ih.search(SearchQuery("React", "Bangalore", max_pages=1)):
        if j.external_id == job_id:
            return await ih.fetch_details(j)
    raise AssertionError(job_id)


async def test_login_detection(ih, engine, site_server):
    assert await ih.is_logged_in()
    await engine.context.add_cookies([{"name": "ih_logged_out", "value": "1", "url": site_server}])
    assert not await ih.is_logged_in()

    async def login(page):
        await page.fill("#email", "me@example.com")
        await page.fill("#password", "x")
        await page.click("#login")
        await page.wait_for_url("**/candidate/opportunities/**")
        return GateResult.RESOLVED

    await ih.ensure_logged_in(FakeGate([login]))
    assert await ih.is_logged_in()


async def test_search_dedupes_opportunities_and_search(ih):
    jobs = [j async for j in ih.search(SearchQuery("React", "Bangalore", experience_years=7))]
    assert [j.external_id for j in jobs] == ["5001", "5002", "5003", "5004"]
    assert jobs[0].company == "Nova Payments" and jobs[0].title == "Frontend Developer"
    assert jobs[0].skills == ["React", "TypeScript"] and jobs[2].already_applied
    assert "skills=React" in ih.search_url(SearchQuery("React", "Bangalore", experience_years=7))


async def test_details(ih):
    job = await _job(ih, "5001")
    assert "Requirements" in job.description and job.apply_type == "portal"
    assert job.experience_text == "4 - 8 years"


async def test_apply_with_modal(ih, engine, profile):
    job = await _job(ih, "5001")
    ctx = make_ctx(engine, profile, job, AgentMode.AUTO)
    res = await ih.apply(job, ctx)
    assert res.outcome == ApplyOutcome.APPLIED, res.message
    assert await engine.page.evaluate("() => window.ECTC") == "38"


async def test_one_click_apply_and_dry_run(ih, engine, profile):
    job = await _job(ih, "5002")
    ctx = make_ctx(engine, profile, job, AgentMode.DRY_RUN)
    assert (await ih.apply(job, ctx)).outcome == ApplyOutcome.DRY_RUN and ctx.submits == []
    res = await ih.apply(job, make_ctx(engine, profile, job, AgentMode.AUTO))
    assert res.outcome == ApplyOutcome.APPLIED and "successfully applied" in res.confirmation_text


async def test_review_mode_approval(ih, engine, profile):
    job = await _job(ih, "5002")
    gate = FakeGate([GateResult.APPROVE])
    res = await ih.apply(job, make_ctx(engine, profile, job, AgentMode.REVIEW, gate))
    assert res.outcome == ApplyOutcome.APPLIED and gate.calls[0][0] == "review"


async def test_already_applied(ih, engine, profile):
    job = await _job(ih, "5003")
    res = await ih.apply(job, make_ctx(engine, profile, job, AgentMode.AUTO))
    assert res.outcome == ApplyOutcome.ALREADY_APPLIED
