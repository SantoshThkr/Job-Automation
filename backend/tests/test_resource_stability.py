"""Long-run resource stability (Goal 4): 24 applications in one run, one browser, mixed flows - company-site
redirects in new tabs, Naukri screening chats, a lost Apply click, LinkedIn 2026 Easy Apply dialogs, Instahyre apply
modals. After every application the run must be back at its baseline: one context, one page, no extra Chromium
processes, no growing listeners / asyncio tasks; Python memory must not keep climbing."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.browser.engine import BrowserEngine
from app.db import session_scope
from app.engine.recorder import remember_answer, set_status, upsert_job
from app.models import ApplicationEvent, Job, JobStatus
from app.portals.types import JobPosting

pytestmark = pytest.mark.browser

FLOWS = [  # (portal, url path, external id prefix)
    ("naukri", "/naukri/job-listings-frontend-engineer-kitco-bengaluru-10162000000", "ext"),  # -> company site tab
    ("naukri", "/naukri/job-listings-frontend-engineer-kitco-bengaluru-10152000000", "chat"),
    ("linkedin", "/linkedin-new/jobs/view/4465524749/", "li"),
    ("instahyre", "/instahyre/job-5005-frontend-engineer-at-kitco-bangalore/", "ih"),
    ("naukri", "/naukri/job-listings-react-developer-lazy-pages-bengaluru-10172000000", "lost"),
    ("naukri", "/naukri/job-listings-ui-engineer-react-gamma-health-bengaluru-10032000000", "gh"),  # -> Greenhouse tab
]


def _seed(site_server, n: int) -> list[int]:
    ids = []
    with session_scope() as s:
        for q, a in [("What is your blood group?", "O+"), ("Work Authorization", "Citizen"),
                     ("GraphQL experience (years)", "2")]:
            remember_answer(s, q, a)
        for i in range(n):
            portal, path, prefix = FLOWS[i % len(FLOWS)]
            ext = path.rstrip("/").split("-")[-1] if portal == "naukri" else path.rstrip("/").split("/")[-1].split("-")[1] \
                if portal == "instahyre" else path.rstrip("/").split("/")[-1]
            job, _ = upsert_job(s, JobPosting(portal=portal, external_id=f"{ext}" if i < len(FLOWS) else f"{ext}-{i}",
                                              url=f"{site_server}{path}", title=f"Frontend Engineer {i}",
                                              company=f"KitCo {i}", location="Bengaluru",
                                              description="React TypeScript Next.js Redux Jest. 5+ years."))
            job.match_score, job.decision = 90.0 - i * 0.1, "APPLY"
            set_status(s, job, JobStatus.QUEUED, "Relevant")
            ids.append(job.id)
    return ids


async def test_resources_return_to_baseline_over_24_applications(site_server, tmp_path, clean_db, monkeypatch):
    from app.engine.control import AgentControl
    from app.engine.orchestrator import Orchestrator
    from app.models import AgentState
    from app.portals.registry import PORTALS

    n = 24
    _seed(site_server, n)
    # the test profile caps a run at 10 applications: this run needs all 24
    monkeypatch.setattr(Orchestrator, "_limits_reached", lambda self: "" if self.stats["apply_attempts"] < n else "cap")

    def make(name, eng):
        base = {"linkedin": f"{site_server}/linkedin-new"}.get(name, f"{site_server}/{name}")
        adapter = PORTALS[name](eng, base)

        async def nothing(_q):
            return
            yield

        adapter.search = nothing  # type: ignore[method-assign]
        return adapter

    engine = BrowserEngine(headless=True, user_data_dir=tmp_path / "p", screenshot_dir=tmp_path / "s",
                           human_delays=False)
    await engine.start()
    await engine.context.add_cookies([{"name": "li_at", "value": "AQEDAS-test", "url": site_server}])
    with session_scope() as s:
        st = AgentState.get(s)
        st.mode, st.portals, st.desired_state = "auto", ["naukri", "linkedin", "instahyre"], "RUNNING"
    control = AgentControl(cache_s=0)
    engine._checkpoint = control.checkpoint
    try:
        summary = await Orchestrator(engine, control, adapter_factory=make).run()
    finally:
        await engine.stop()
    assert summary["stats"]["apply_attempts"] == n, summary

    with session_scope() as s:
        samples = [e.data for e in s.scalars(select(ApplicationEvent).where(ApplicationEvent.type == "resource")
                                             .order_by(ApplicationEvent.id))]
        outcomes = [j.status for j in s.scalars(select(Job))]
    after = [d for d in samples if d.get("phase") == "after"]
    assert len(after) == n
    table = {k: after[k - 1] for k in (1, 5, 10, 15, 20, 24)}
    for k, d in table.items():
        print(f"after {k:2d}: pages={d['pages']} chromium_processes={d['chromium_processes']} "
              f"chromium_rss_mb={d['chromium_rss_mb']:.0f} renderer_max_mb={d['renderer_max_mb']:.0f} "
              f"python_rss_mb={d['python_rss_mb']:.0f} listeners={d['listeners']} tasks={d['tasks']}")
    print("outcomes:", {o: outcomes.count(o) for o in set(outcomes)})

    # every application ends with the run's single tab - no abandoned tabs
    assert all(d["pages"] == 1 and d["contexts"] == 1 and d["browsers"] == 1 for d in after), \
        [(i + 1, d["pages"], d["urls"]) for i, d in enumerate(after) if d["pages"] != 1]
    # nothing accumulates on that tab or in the event loop
    first, last = after[4], after[-1]  # after warm-up (first flows of each kind)
    assert last["listeners"] <= first["listeners"] + 2, (first["listeners"], last["listeners"])
    assert last["tasks"] <= first["tasks"] + 3, (first["tasks"], last["tasks"])
    assert last["chromium_processes"] <= max(d["chromium_processes"] for d in after[:6]) + 2
    # memory does not keep climbing: the second half grows far less than the first
    py = [d["python_rss_mb"] for d in after]
    assert py[-1] - py[len(py) // 2] < 60, py


async def test_work_tab_survives_a_page_calling_window_close(site_server, tmp_path):
    """A page's window.close() never closes the run's work tab (Chrome only lets scripts close tabs they opened);
    a popup the site opened itself still closes normally."""
    import asyncio

    engine = BrowserEngine(headless=True, user_data_dir=tmp_path / "p", screenshot_dir=tmp_path / "s", human_delays=False)
    await engine.start()
    try:
        page = await engine.fresh_page()
        await engine.goto(f"{site_server}/submit-lab/selfclose.html")
        await asyncio.sleep(1.5)
        assert not page.is_closed() and len(engine.context.pages) == 1
        async with engine.context.expect_page() as info:
            await page.evaluate(f"() => window.open('{site_server}/submit-lab/selfclose.html')")
        popup = await info.value
        await asyncio.sleep(1.5)
        assert popup.is_closed() and not page.is_closed()
    finally:
        await engine.stop()


async def test_a_closed_work_tab_is_replaced_at_the_application_boundary(site_server, tmp_path):
    """Whatever closes the work tab mid-application, the run continues with exactly one live tab."""
    engine = BrowserEngine(headless=True, user_data_dir=tmp_path / "p", screenshot_dir=tmp_path / "s", human_delays=False)
    await engine.start()
    try:
        page = await engine.fresh_page()
        await page.close()  # e.g. closed by hand in the visible window
        await engine.ensure_alive()
        assert len(engine.context.pages) == 1 and not engine.page.is_closed()
    finally:
        await engine.stop()
