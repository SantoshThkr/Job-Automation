"""A portal that stops taking applications is the portal's state, not a problem with each job.

Naukri did this for hours on 2026-10-09: 14 applications in a row ended as "did not register the Apply click" and
were each parked for the user. Nothing had been sent, and the same jobs applied normally later. Now the reason is
read (the refused request, a toast), such a job stays in the queue, and the portal's own applications pause for the
rest of the run while company-site applications continue.

Also here: the run log says when the local AI model cannot be used (it was silently unreachable for three runs:
"localhost" resolved to another server that did not have the model).
"""

from __future__ import annotations


import httpx
import pytest
from sqlalchemy import select

from app.ai.provider import OllamaProvider
from app.browser.engine import BrowserEngine
from app.db import session_scope
from app.engine.control import AgentControl
from app.engine.orchestrator import Orchestrator
from app.engine.submission import _json_message
from app.models import AgentMode, AgentState, ApplicationEvent, Job, JobStatus
from app.portals.registry import PORTALS
from app.portals.types import ApplyOutcome, ApplyResult, JobPosting
from tests.helpers import FakeGate, make_ctx

LIMIT = "You have exhausted your daily apply limit. Please try again tomorrow."


# ------------------------------------------------------------------ what the site said

@pytest.mark.parametrize("body,said", [
    ('{"message": "Daily limit reached"}', "Daily limit reached"),
    ('{"error": {"message": "You have exhausted your daily apply limit."}}', "You have exhausted your daily apply limit."),
    ('{"errors": [{"detail": "Profile is  incomplete"}]}', "Profile is incomplete"),
    ('{"status": 429, "statusMessage": "Too many requests"}', "Too many requests"),
    ('{"ok": true}', ""), ('{"message": ""}', ""), ("<html>Bad gateway</html>", ""), ("", ""),
    ('{"a": {"b": {"c": {"message": "too deep to be the answer"}}}}', ""),
])
def test_json_message(body, said):
    assert _json_message(body) == said


def test_json_message_is_bounded():
    assert len(_json_message('{"message": "' + "x" * 380 + '"}')) == 300
    assert _json_message('{"message": "' + "x" * 5000 + '"}') == ""  # not a message: a document


# ------------------------------------------------------------------ the local AI model: found, or said to be missing

def _ollama(models_by_host: dict[str, list[str]], url: str, model: str = "qwen3:4b") -> OllamaProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        models = models_by_host.get(request.url.host)
        if models is None:
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json={"models": [{"name": m} for m in models]})

    return OllamaProvider(base_url=url, model=model, enabled=True, transport=httpx.MockTransport(handler))


async def test_model_on_the_other_loopback_address_is_found():
    """'localhost' reached another project's server (::1) that lacks the model; the real one listens on 127.0.0.1."""
    provider = _ollama({"localhost": ["nomic-embed-text"], "127.0.0.1": ["qwen3:4b"]}, "http://localhost:11434")
    health = await provider.health(use_cache=False)
    assert health["available"] and health["model_present"] and health["url"] == "http://127.0.0.1:11434"
    assert provider.base_url == "http://127.0.0.1:11434"  # later requests go where the model is


async def test_a_missing_model_is_reported_with_what_to_do():
    provider = _ollama({"127.0.0.1": ["nomic-embed-text"], "localhost": ["nomic-embed-text"]}, "http://127.0.0.1:11434")
    health = await provider.health(use_cache=False)
    assert health["available"] and not health["model_present"]
    assert "ollama pull qwen3:4b" in health["error"] and "nomic-embed-text" in health["error"]
    assert provider.base_url == "http://127.0.0.1:11434"


async def test_a_remote_server_is_never_swapped_for_a_local_one():
    provider = _ollama({"127.0.0.1": ["qwen3:4b"], "ai.internal.test": ["other"]}, "http://ai.internal.test:11434")
    health = await provider.health(use_cache=False)
    assert not health["model_present"] and provider.base_url == "http://ai.internal.test:11434"


async def test_nothing_reachable_is_unavailable_not_an_exception():
    health = await _ollama({}, "http://localhost:11434").health(use_cache=False)
    assert not health["available"] and not health["model_present"] and health["error"]


# ------------------------------------------------------------------ the run: pause the portal, keep the jobs

@pytest.fixture
async def engine(site_server, tmp_path):
    e = BrowserEngine(headless=True, user_data_dir=tmp_path / "p", screenshot_dir=tmp_path / "s", human_delays=False)
    yield e
    await e.stop()


def _events(kind: str) -> list[ApplicationEvent]:
    with session_scope() as s:
        return list(s.scalars(select(ApplicationEvent).where(ApplicationEvent.type == kind).order_by(ApplicationEvent.id)))


async def _run(engine, site_server, wrap, *, assistant=None):
    with session_scope() as s:
        st = AgentState.get(s)
        st.mode, st.portals, st.desired_state = "auto", ["naukri"], "RUNNING"
    control = AgentControl(cache_s=0)
    engine._checkpoint = control.checkpoint

    def make(name, eng):
        cls = PORTALS.get(name)
        return wrap(cls(eng, f"{site_server}/{name}")) if cls else None

    orch = Orchestrator(engine, control, assistant=assistant, adapter_factory=make)
    return await orch.run(), orch


def _refusing(result_for):
    """Naukri's own applications answer with ``result_for(n)`` (n = 1, 2, ...); company-site ones run normally."""
    calls: list[str] = []

    def wrap(adapter):
        original = adapter.apply

        async def apply(job, ctx):
            if job.apply_type == "external":
                return await original(job, ctx)
            calls.append(job.external_id)
            await ctx.mark_submit_attempt()  # the click was made...
            result = result_for(len(calls))
            return result if result is not None else await original(job, ctx)

        adapter.apply = apply
        return adapter

    return wrap, calls


def _natives() -> dict[str, tuple[str, str, bool]]:
    with session_scope() as s:
        return {j.external_id: (j.status, j.status_reason or "", bool(j.application and j.application.submit_attempted))
                for j in s.scalars(select(Job).where(Job.portal == "naukri")) if j.apply_type != "external"
                and j.status not in {JobStatus.SKIPPED, JobStatus.DUPLICATE, JobStatus.ALREADY_APPLIED}}  # never candidates


@pytest.mark.browser
async def test_a_portal_limit_keeps_jobs_queued_and_pauses_only_that_portals_own_applications(engine, site_server, clean_db):
    wrap, calls = _refusing(lambda n: ApplyResult(
        ApplyOutcome.MANUAL_ACTION_REQUIRED, f"Naukri is not accepting more applications right now ({LIMIT}) - nothing was sent",
        submit_attempted=False, portal_limit=LIMIT, nothing_sent=True))
    summary, orch = await _run(engine, site_server, wrap)
    assert summary["status"] == "COMPLETED", summary
    assert len(calls) == 1  # asked once; the other jobs were not even tried
    natives = _natives()
    assert len(natives) >= 3 and all(status == "QUEUED" for status, _, _ in natives.values()), natives
    status, reason, attempted = natives[calls[0]]
    assert "Kept in the queue" in reason and LIMIT in reason and not attempted  # nothing was sent: Retry is not blocked
    assert not any(s == "MANUAL_ACTION_REQUIRED" for s, _, _ in natives.values())  # nothing is parked on you
    said = _events("portal_limit")
    assert len(said) == 1 and said[0].level == "warning" and LIMIT in said[0].message
    assert "company-site applications continue" in said[0].message
    with session_scope() as s:  # ...and they did: the company-site application of this run went through
        external = [j for j in s.scalars(select(Job).where(Job.portal == "naukri")) if j.apply_type == "external"]
        assert external and any(j.status == JobStatus.APPLIED for j in external), [(j.external_id, j.status) for j in external]
    assert orch.portal_limits == {"naukri": LIMIT}


@pytest.mark.browser
async def test_two_unregistered_clicks_in_a_row_pause_the_portal_and_requeue_both(engine, site_server, clean_db):
    wrap, calls = _refusing(lambda n: ApplyResult(
        ApplyOutcome.MANUAL_ACTION_REQUIRED, "Naukri did not register the Apply click (after reloading, the job still shows "
        "'Apply') - nothing was sent; Retry applies again", submit_attempted=False, nothing_sent=True, click_ignored=True))
    summary, orch = await _run(engine, site_server, wrap)
    assert summary["status"] == "COMPLETED", summary
    assert len(calls) == 2  # two in a row is the portal, not the jobs: the third was not tried
    natives = _natives()
    assert all(status == "QUEUED" for status, _, _ in natives.values()), natives
    assert all("stopped registering Apply clicks" in natives[c][1] and not natives[c][2] for c in calls)
    assert len(_events("requeued")) == 2 and len(_events("portal_limit")) == 1  # each job's timeline says why it is back
    assert summary["stats"].get("result_manual_action_required", 0) == 0
    assert "naukri" in orch.portal_limits


@pytest.mark.browser
async def test_one_unregistered_click_is_that_jobs_own_problem(engine, site_server, clean_db):
    def result_for(n):
        if n == 1:
            return ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED, "Naukri did not register the Apply click - nothing was "
                               "sent; Retry applies again", submit_attempted=False, nothing_sent=True, click_ignored=True)
        return None  # the next ones apply normally

    wrap, calls = _refusing(result_for)
    summary, orch = await _run(engine, site_server, wrap)
    assert summary["status"] == "COMPLETED" and len(calls) >= 3 and orch.portal_limits == {}
    natives = _natives()
    assert natives[calls[0]][0] == "MANUAL_ACTION_REQUIRED" and not natives[calls[0]][2]  # yours to retry; not "submitted"
    assert sum(status == "APPLIED" for status, _, _ in natives.values()) >= 2
    assert _events("portal_limit") == []


# ------------------------------------------------------------------ the Naukri adapter reads the refusal

@pytest.mark.browser
async def test_naukri_refused_apply_request_is_read_as_a_limit(engine, site_server, profile):
    await engine.start()
    nk = PORTALS["naukri"](engine, f"{site_server}/naukri")
    job = JobPosting(portal="naukri", external_id="10173000000", title="React Developer", company="Quota Works",
                     url=f"{site_server}/naukri/job-listings-react-developer-quota-works-bengaluru-10173000000")
    ctx = make_ctx(engine, profile, job, AgentMode.AUTO, FakeGate())
    res = await nk.apply(job, ctx)
    assert res.outcome == ApplyOutcome.MANUAL_ACTION_REQUIRED and res.nothing_sent and not res.submit_attempted
    assert res.portal_limit == LIMIT and "not accepting more applications right now" in res.message
    said = " | ".join(res.evidence)
    assert "-> 429" in said and LIMIT in said  # the refused request and what it answered
    assert "shown: " + LIMIT in said  # the toast, although it was gone again before the page was read
    assert any(t == "apply_not_registered" for t, _ in ctx.events)


@pytest.mark.browser
async def test_naukri_silently_swallowed_click_is_not_called_a_limit(engine, site_server, profile):
    await engine.start()
    nk = PORTALS["naukri"](engine, f"{site_server}/naukri")
    job = JobPosting(portal="naukri", external_id="10174000000", title="React Developer", company="Still Waters",
                     url=f"{site_server}/naukri/job-listings-react-developer-still-waters-bengaluru-10174000000")
    ctx = make_ctx(engine, profile, job, AgentMode.AUTO, FakeGate())
    res = await nk.apply(job, ctx)
    assert res.outcome == ApplyOutcome.MANUAL_ACTION_REQUIRED and res.nothing_sent and not res.submit_attempted
    assert res.portal_limit == "" and "did not register the Apply click" in res.message
    assert "no request was sent and nothing appeared on the page" in " | ".join(res.evidence)
    assert [t for t, _ in ctx.events].count("apply_retry") == 1  # clicked again once, then left alone
    assert res.verification != "confirmed"


# ------------------------------------------------------------------ the run log names an unusable model

class _Assistant:
    def __init__(self, provider):
        self.provider = provider

    async def available(self) -> bool:
        return await self.provider.is_available()


@pytest.mark.browser
async def test_run_log_says_when_the_local_ai_cannot_be_used(engine, site_server, clean_db):
    wrap, _ = _refusing(lambda n: ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED, "paused", submit_attempted=False,
                                              portal_limit="test pause", nothing_sent=True))
    missing = _ollama({"127.0.0.1": ["nomic-embed-text"]}, "http://127.0.0.1:11434")
    summary, _orch = await _run(engine, site_server, wrap, assistant=_Assistant(missing))
    warned = _events("ai_unavailable")
    assert len(warned) == 1 and warned[0].level == "warning" and "ollama pull qwen3:4b" in warned[0].message
    assert "left for you" in warned[0].message and summary["stats"].get("ai_unavailable") == 1
    assert _events("ai_status") == []


# ------------------------------------------------------------------ the portal's "Applied" badge on a company-site job

def _parked_company_site_job(site_server, final_url: str, *, submit_attempted: bool = False, ext: str = "10042000000"):
    """A Naukri job whose application moved to a company site and stopped there (the mock job page shows "Applied",
    as Naukri does once the company-site link has been opened)."""
    from app.engine.recorder import set_status, start_application, upsert_job

    with session_scope() as s:
        job, _ = upsert_job(s, JobPosting(portal="naukri", external_id=ext, title="Frontend Engineer", company="LabCo",
                                          url=f"{site_server}/naukri/job-listings-frontend-engineer-labco-bengaluru-{ext}",
                                          location="Bengaluru", description="React TypeScript", apply_type="external"))
        app = start_application(s, job, AgentMode.AUTO, resume_id=None, resume_path=None)
        app.apply_target, app.final_url, app.submit_attempted = "external", final_url, submit_attempted
        app.manual_action_reason = "Form errors: Please provide your place of residence"
        set_status(s, job, JobStatus.QUEUED, "Queued for retry")
        return job.id


def test_unfinished_elsewhere(clean_db, site_server):
    from app.engine.orchestrator import _unfinished_elsewhere

    company_site = site_server.replace("127.0.0.1", "localhost") + "/submit-lab/form.html?mode=message"
    cases = [(company_site, False, True),  # moved to the company's site, nothing sent: its application is unfinished
             (company_site, True, False),  # something was sent there: never continued (never sent twice)
             (f"{site_server}/naukri/job-listings-x-1", False, False),  # still on the portal itself
             ("javascript:alert(1)", False, False), ("", False, False)]
    for n, (url, attempted, expected) in enumerate(cases):
        job_id = _parked_company_site_job(site_server, url, submit_attempted=attempted, ext=f"1004200000{n}")
        with session_scope() as s:
            assert _unfinished_elsewhere(s.get(Job, job_id)) is expected, (url, attempted)


@pytest.mark.browser
async def test_a_parked_company_site_application_continues_there_despite_the_portals_applied_badge(engine, site_server, clean_db):
    """Naukri (run #28): the retry of a parked SmartRecruiters application went back through Naukri, saw "Applied"
    (shown as soon as the company-site link has been opened) and closed the job as already applied. Ten older jobs
    had ended the same way without their company-site application ever being sent."""
    company_site = site_server.replace("127.0.0.1", "localhost") + "/submit-lab/form.html?mode=message"
    job_id = _parked_company_site_job(site_server, company_site)
    asked: list[str] = []

    def wrap(adapter):
        original = adapter.apply

        async def apply(job, ctx):
            asked.append(job.external_id)
            return await original(job, ctx)

        adapter.apply = apply
        return adapter

    with session_scope() as s:
        st = AgentState.get(s)
        st.mode, st.portals, st.desired_state = "auto", ["naukri"], "RUNNING"
    control = AgentControl(cache_s=0)
    engine._checkpoint = control.checkpoint
    orch = Orchestrator(engine, control, adapter_factory=lambda n, e: wrap(PORTALS[n](e, f"{site_server}/{n}")))
    orch.max_applications = 1
    summary = await orch.run()
    assert summary["status"] == "COMPLETED", summary
    with session_scope() as s:
        job = s.get(Job, job_id)
        assert job.status == JobStatus.APPLIED, (job.status, job.status_reason)  # sent on the company site, confirmed there
        assert job.application.confirmed and "received" in job.application.confirmation_text
    assert "10042000000" not in asked  # the portal page (and its badge) was never consulted for it


@pytest.mark.browser
async def test_two_question_stops_in_a_row_are_not_a_portal_problem(engine, site_server, clean_db):
    """A screening question that stopped an application (nothing sent) is that job's matter - only Apply clicks the
    portal ignored count toward pausing it."""
    wrap, calls = _refusing(lambda n: ApplyResult(
        ApplyOutcome.MANUAL_ACTION_REQUIRED, "Naukri screening question needs your answer: 'PAN Number'",
        submit_attempted=False, nothing_sent=True) if n <= 2 else None)
    summary, orch = await _run(engine, site_server, wrap)
    assert summary["status"] == "COMPLETED" and len(calls) >= 3 and orch.portal_limits == {}
    assert _events("portal_limit") == [] and _events("requeued") == []
    natives = _natives()
    assert [natives[c][0] for c in calls[:2]] == ["MANUAL_ACTION_REQUIRED"] * 2
    assert not natives[calls[0]][2]  # nothing was sent: it can be retried once the answer is known


# ------------------------------------------------------------------ Retry after something may have been sent

def test_retry_still_needs_an_explicit_force_when_a_submit_may_have_gone_out(clean_db, site_server):
    """Unchanged safety: only a verified "nothing was sent" lets a job be retried (or re-queued) without asking."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    sent = _parked_company_site_job(site_server, "https://careers.example.org/apply/1", submit_attempted=True, ext="20000000001")
    unsent = _parked_company_site_job(site_server, "https://careers.example.org/apply/2", submit_attempted=False, ext="20000000002")
    with session_scope() as s:
        from app.engine.recorder import set_status

        for job_id in (sent, unsent):
            set_status(s, s.get(Job, job_id), JobStatus.MANUAL_ACTION_REQUIRED, "Submitted - confirmation not verified")
    with TestClient(create_app()) as c:
        refused = c.post(f"/api/jobs/{sent}/action", json={"action": "retry"})
        assert refused.status_code == 409 and "already attempted" in refused.json()["detail"]
        assert c.get(f"/api/jobs/{sent}").json()["status"] == "MANUAL_ACTION_REQUIRED"  # nothing changed
        assert c.post(f"/api/jobs/{sent}/action", json={"action": "retry", "force": True}).json()["status"] == "QUEUED"
        assert c.post(f"/api/jobs/{unsent}/action", json={"action": "retry"}).json()["status"] == "QUEUED"


# ------------------------------------------------------------------ a portal session that ends in the middle of a run

def _session_ends(at: int, *, after_submit: bool = False):
    """From the ``at``-th application on, the portal answers with its login page."""
    from app.browser.errors import LoginRequired

    calls: list[str] = []

    def wrap(adapter):
        original = adapter.apply

        async def apply(job, ctx):
            calls.append(job.external_id)
            if len(calls) >= at:
                if after_submit:
                    await ctx.mark_submit_attempt()
                raise LoginRequired("naukri", "Naukri session expired (redirected to login)")
            return await original(job, ctx)

        adapter.apply = apply
        return adapter

    return wrap, calls


def _statuses() -> dict[str, tuple[str, str]]:
    with session_scope() as s:
        return {j.external_id: (j.status, j.status_reason or "") for j in s.scalars(select(Job).where(Job.portal == "naukri"))}


@pytest.mark.browser
async def test_an_expired_portal_session_keeps_its_jobs_queued(engine, site_server, clean_db):
    """The session ends at the second application. That job is not applied, not skipped and not parked on you: it
    is back in the queue with the reason; the portal's remaining jobs are not even opened; it is said once."""
    wrap, calls = _session_ends(2)
    summary, orch = await _run(engine, site_server, wrap)
    assert summary["status"] == "COMPLETED", summary
    assert len(calls) == 2  # after the session ended no further job of the portal was tried
    after = _statuses()
    status, reason = after[calls[1]]
    assert status == JobStatus.QUEUED and "session expired" in reason and "log in to naukri again" in reason
    assert not any(st in {JobStatus.SKIPPED, JobStatus.FAILED} and "session" in why.lower() for st, why in after.values())
    assert not any("session expired" in why for st, why in after.values() if st == JobStatus.MANUAL_ACTION_REQUIRED)
    assert sum(1 for st, _ in after.values() if st == JobStatus.QUEUED) >= 2  # the others wait too
    said = _events("login_required")
    assert len(said) == 1 and "make login-naukri" in said[0].message and "Nothing was skipped" in said[0].message
    assert len([e for e in _events("requeued") if "session expired" in e.message]) == 1
    assert orch.logged_out == {"naukri": "Naukri session expired (redirected to login)"}
    with session_scope() as s:
        job = s.scalar(select(Job).where(Job.external_id == calls[1]))
        assert job.application is None or not (job.application.confirmed or job.application.submit_attempted)


@pytest.mark.browser
async def test_a_session_that_ends_after_the_submit_click_is_yours_to_verify(engine, site_server, clean_db):
    """Something may have been sent: that job is never put back to be applied to again by itself."""
    wrap, calls = _session_ends(1, after_submit=True)
    summary, _orch = await _run(engine, site_server, wrap)
    assert summary["status"] == "COMPLETED", summary
    status, reason = _statuses()[calls[0]]
    assert status == JobStatus.MANUAL_ACTION_REQUIRED and "session expired" in reason
    with session_scope() as s:
        job = s.scalar(select(Job).where(Job.external_id == calls[0]))
        assert job.application.submit_attempted and not job.application.confirmed
    assert len(calls) == 1 and len(_events("login_required")) == 1


@pytest.mark.browser
async def test_the_next_run_tries_the_portal_again(engine, site_server, clean_db):
    wrap, calls = _session_ends(1)
    _summary, orch = await _run(engine, site_server, wrap)
    assert orch.logged_out and len(calls) == 1
    first = calls[0]
    wrap2, calls2 = _session_ends(10**6)  # you logged in again: nothing ends the session now
    summary2, orch2 = await _run(engine, site_server, wrap2)
    assert summary2["status"] == "COMPLETED" and orch2.logged_out == {}
    assert first in calls2 and _statuses()[first][0] != JobStatus.QUEUED  # the requeued job got its turn


# ------------------------------------------------------------------ an attempt that broke for a passing reason

def _breaks(make_error, *, times: int = 1, after_submit: bool = False):
    """The first native application of each run breaks with ``make_error()`` (``times`` runs in a row)."""
    state = {"left": times, "job": ""}
    calls: list[str] = []

    def wrap(adapter):
        original = adapter.apply

        async def apply(job, ctx):
            if job.apply_type == "external":
                return await original(job, ctx)
            calls.append(job.external_id)
            victim = state["job"] or job.external_id
            if job.external_id == victim and state["left"] > 0:
                state["job"], state["left"] = victim, state["left"] - 1
                if after_submit:
                    await ctx.mark_submit_attempt()
                raise make_error()
            return await original(job, ctx)

        adapter.apply = apply
        return adapter

    return wrap, calls, state


def _offline():
    from app.browser.errors import NavigationFailed

    return NavigationFailed("Could not load https://www.naukri.test/job-1: Page.goto: net::ERR_INTERNET_DISCONNECTED")


@pytest.mark.browser
async def test_a_dropped_connection_keeps_the_job_in_the_queue(engine, site_server, clean_db):
    """Thirteen jobs had been recorded as Failed because the internet dropped (or the browser window was closed)
    in the middle of an application. Nothing about the job failed and nothing was sent: it waits in the queue."""
    wrap, calls, state = _breaks(_offline)
    summary, _orch = await _run(engine, site_server, wrap)
    assert summary["status"] == "COMPLETED", summary
    status, reason = _statuses()[state["job"]]
    assert status == JobStatus.QUEUED and "the internet connection dropped" in reason and "nothing was sent" in reason
    assert summary["stats"].get("result_failed", 0) == 0 and len(calls) >= 2  # the run went on with the next job
    assert len([e for e in _events("requeued") if "connection dropped" in e.message]) == 1
    with session_scope() as s:
        job = s.scalar(select(Job).where(Job.external_id == state["job"]))
        assert job.application.attempts == 1 and not job.application.submit_attempted and not job.application.failure_reason


@pytest.mark.browser
async def test_the_third_broken_attempt_is_recorded_as_failed(engine, site_server, clean_db):
    wrap, _calls, state = _breaks(_offline, times=3)
    for expected in (JobStatus.QUEUED, JobStatus.QUEUED, JobStatus.FAILED):
        await _run(engine, site_server, wrap)
        status, reason = _statuses()[state["job"]]
        assert status == expected, (status, reason)
    assert "broke 3 times" in reason and "Retry" in reason  # no endless loop: yours to retry when it is stable


@pytest.mark.browser
async def test_a_connection_lost_after_the_submit_click_is_never_requeued(engine, site_server, clean_db):
    wrap, _calls, state = _breaks(_offline, after_submit=True)
    await _run(engine, site_server, wrap)
    status, _reason = _statuses()[state["job"]]
    assert status != JobStatus.QUEUED  # something may have been sent: it is not applied to again by itself
    with session_scope() as s:
        assert s.scalar(select(Job).where(Job.external_id == state["job"])).application.submit_attempted


def test_which_failures_pass_by_themselves():
    from playwright.async_api import Error as PlaywrightError

    from app.browser.errors import BrowserCrashed, NavigationFailed
    from app.engine.orchestrator import transient_failure

    assert transient_failure(_offline()) == "the internet connection dropped"
    assert transient_failure(NavigationFailed("Page.goto: net::ERR_NAME_NOT_RESOLVED")) == "the internet connection dropped"
    assert transient_failure(BrowserCrashed("gone")) == "the browser window was closed"
    assert transient_failure(PlaywrightError("Page.wait_for_timeout: Target page, context or browser has been closed")) \
        == "the browser window was closed"
    for lasting in (ValueError("bad"), NavigationFailed("Could not load: Page.goto: Timeout 30000ms exceeded."),
                    RuntimeError("Could not click <Locator>")):
        assert transient_failure(lasting) == ""
