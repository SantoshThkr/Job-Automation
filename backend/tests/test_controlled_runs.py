"""A run you can control, and an API you can tell apart.

* ``agent --jobs 12,34``: exactly those queued jobs - no search, nothing else from the queue, no skipped job looked at
  again. ``--max-searches N``: a search that stops after N query / location pairs. (For reviewing a batch before it is
  applied to, and for trying a portal without a two-hour search.)
* A listing that could not be read, or that its portal had paused, is read again when a later search lists it.
* ``/api/health`` says which service is answering, and ``check`` reports a port shared with another program - on
  2026-10-13 another project's container answered on this API's port whenever the API was down.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import cli
from app.db import session_scope
from app.engine.control import AgentControl
from app.engine.orchestrator import Orchestrator, _worth_another_look
from app.engine.recorder import set_status, start_application, upsert_job
from app.models import AgentMode, AgentState, ApplicationEvent, Job, JobStatus, utcnow
from app.portals.types import ApplyOutcome, ApplyResult, JobPosting

FRONTEND_JD = ("We are hiring a frontend engineer.\nRequirements:\n- React, TypeScript, Redux, HTML, CSS\n- Next.js\n"
               "- Jest and React Testing Library\n- REST APIs, performance tuning, accessibility\n" * 2)


def _posting(ext: str, portal: str = "naukri", title: str = "Senior Frontend Engineer", **kw) -> JobPosting:
    kw.setdefault("posted_text", "1 day ago")  # a fresh posting: these tests are not about its age
    return JobPosting(portal=portal, external_id=ext, url=f"https://{portal}.test/{ext}", title=title,
                      company=f"Co {ext}", location="Bengaluru", experience_text="5-8 Yrs", description=FRONTEND_JD, **kw)


class _Engine:
    alive = False
    user_data_dir = "/nonexistent"

    def __init__(self):
        self.started = 0

    async def start(self):
        self.started += 1


class _Portal:
    name = "naukri"
    display_name = "Naukri"

    def __init__(self):
        self.searched: list[str] = []

    async def ensure_logged_in(self, gate):
        return None

    async def search(self, q):
        self.searched.append(f"{q.keywords} / {q.location}")
        return
        yield  # (an async generator that finds nothing)

    async def fetch_details(self, posting):
        return posting


@pytest.fixture
def orch(clean_db):
    with session_scope() as s:
        st = AgentState.get(s)
        st.mode, st.portals, st.desired_state = "auto", ["naukri"], "RUNNING"
    portal = _Portal()
    o = Orchestrator(_Engine(), AgentControl(cache_s=0), adapter_factory=lambda n, e: portal)
    o.portal = portal
    return o


def _queue(n: int) -> list[int]:
    ids = []
    with session_scope() as s:
        for i in range(n):
            job, _ = upsert_job(s, _posting(f"c-{i}"))
            job.match_score = 90.0 - i
            set_status(s, job, JobStatus.QUEUED, f"Relevant (score {job.match_score})")
            ids.append(job.id)
    return ids


def _events(kind: str) -> list[str]:
    with session_scope() as s:
        return [e.message for e in s.scalars(select(ApplicationEvent).where(ApplicationEvent.type == kind).order_by(ApplicationEvent.id))]


# ------------------------------------------------------------------ exactly the chosen jobs

async def test_a_run_limited_to_chosen_jobs_touches_nothing_else(orch, monkeypatch):
    ids = _queue(6)
    chosen = {ids[4], ids[1]}
    applied: list[int] = []
    looked_again: list[bool] = []

    async def apply_job(job_id, adapter, posting):
        applied.append(job_id)
        with session_scope() as s:
            set_status(s, s.get(Job, job_id), JobStatus.APPLIED, "Applied (test)")
        return ApplyResult(ApplyOutcome.APPLIED, "Applied (test)", submit_attempted=True)

    monkeypatch.setattr(orch, "apply_job", apply_job)
    monkeypatch.setattr(orch, "_reconsider_skipped", lambda: looked_again.append(True))
    orch.only_jobs = chosen
    summary = await orch.run()
    assert summary["status"] == "COMPLETED", summary
    assert applied == [ids[1], ids[4]]  # only the chosen ones, best match first
    with session_scope() as s:
        status = {j.id: j.status for j in s.scalars(select(Job))}
    assert all(status[i] == (JobStatus.APPLIED if i in chosen else JobStatus.QUEUED) for i in ids)
    assert orch.portal.searched == [] and looked_again == []  # no search, no skipped job reconsidered
    assert any("limited to the chosen job" in m for m in _events("limit"))


async def test_a_chosen_job_that_is_not_queued_is_not_applied_to(orch, monkeypatch):
    """Applied meanwhile, skipped by you, or simply a wrong number: naming a job does not put it in the queue."""
    ids = _queue(2)
    with session_scope() as s:
        set_status(s, s.get(Job, ids[0]), JobStatus.APPLIED, "Applied on Naukri")
    applied: list[int] = []

    async def apply_job(job_id, adapter, posting):
        applied.append(job_id)
        return ApplyResult(ApplyOutcome.APPLIED, "x", submit_attempted=True)

    monkeypatch.setattr(orch, "apply_job", apply_job)
    orch.only_jobs = {ids[0], 999_999}
    await orch.run()
    assert applied == []
    with session_scope() as s:
        assert s.get(Job, ids[1]).status == JobStatus.QUEUED


@pytest.mark.parametrize("argv", [["--jobs", "abc"], ["--jobs", ""], ["--jobs", "12,x"], ["--max-searches", "-1"],
                                  ["--search-only", "--jobs", "12"]])
def test_the_agent_refuses_malformed_run_limits(argv, capsys):
    from app import agent

    with pytest.raises(SystemExit) as stop:
        agent.main(["--mode", "dry_run", *argv])
    assert stop.value.code == 2 and ("--jobs" in capsys.readouterr().err or "--max-searches" in argv)


async def test_a_search_only_run_leaves_the_queue_alone(orch, monkeypatch):
    """Finding and evaluating a portal's new listings without touching what is already queued - not even the
    recency check of the queue (that would set old queued jobs aside in bulk)."""
    ids = _queue(2)
    with session_scope() as s:
        old, _ = upsert_job(s, _posting("c-old", posted_at=utcnow() - timedelta(days=40)))
        set_status(s, old, JobStatus.QUEUED, "Relevant (score 80)")
        s.flush()
        ids.append(old.id)
        AgentState.get(s).recency = {"days": 7}
    applied: list[int] = []

    async def apply_job(job_id, adapter, posting):
        applied.append(job_id)
        return ApplyResult(ApplyOutcome.APPLIED, "x", submit_attempted=True)

    monkeypatch.setattr(orch, "apply_job", apply_job)
    orch.search_only = True
    summary = await orch.run()
    assert summary["status"] == "COMPLETED", summary
    assert applied == [] and orch.portal.searched  # it searched; nothing was applied to
    with session_scope() as s:
        assert all(s.get(Job, i).status == JobStatus.QUEUED for i in ids)  # the old one too: left for its own run
    assert any("Search only" in m for m in _events("limit"))


# ------------------------------------------------------------------ a bounded search

async def test_a_search_can_be_limited_to_its_first_queries(orch):
    from app.profile import get_profile_store

    profile = get_profile_store().load(force=True)
    orch.profile = profile.model_copy(update={"search": profile.search.model_copy(update={
        "queries": ["react developer", "frontend developer"], "locations": ["Bengaluru", "Remote", "Pune"]})})
    orch.max_searches = 2
    await orch.discover_portal("naukri")
    assert len(orch.portal.searched) == 2 and any("limited to 2 queries" in m for m in _events("limit"))
    orch.portal.searched.clear()
    orch.max_searches = None
    await orch.discover_portal("naukri")
    assert len(orch.portal.searched) == 6  # without a limit: every query in every location


# ------------------------------------------------------------------ set aside for a reason that may have passed

def _set_aside(ext: str, status: str, reason: str, *, hours_ago: float, with_application: bool = False,
               portal: str = "linkedin") -> int:
    with session_scope() as s:
        job, _ = upsert_job(s, _posting(ext, portal))
        if with_application:
            start_application(s, job, AgentMode.AUTO, resume_id=None, resume_path=None)
        set_status(s, job, status, reason)
        s.flush()
        job.updated_at = utcnow() - timedelta(hours=hours_ago)
        return job.id


@pytest.mark.parametrize("status,reason,hours,application,again", [
    (JobStatus.FAILED, "Error: NavigationFailed: Could not load https://www.linkedin.test/jobs/view/1/: Timeout", 30, False, True),
    (JobStatus.EXPIRED, "LinkedIn: Not currently accepting applications", 30, False, True),
    # too recent: not while the same search is still running, not twice a day
    (JobStatus.FAILED, "Error: NavigationFailed: Timeout", 2, False, False),
    (JobStatus.EXPIRED, "LinkedIn: Not currently accepting applications", 2, False, False),
    # closed for good, decided, applied to, or failed in an application: left as it is
    (JobStatus.EXPIRED, "LinkedIn: No longer accepting applications", 30, False, False),
    (JobStatus.EXPIRED, "The job is closed / no longer accepting applications", 30, False, False),
    (JobStatus.FAILED, "Submission failed on acme.test: Server error", 30, True, False),
    (JobStatus.FAILED, "Error: TimeoutError", 30, True, False),
    (JobStatus.SKIPPED, "Requires 10+ years; you have 7", 30, False, False),
    (JobStatus.SKIPPED, "Skipped by you", 300, False, False),
    (JobStatus.APPLIED, "Applied with LinkedIn Easy Apply", 300, True, False),
    (JobStatus.MANUAL_ACTION_REQUIRED, "Questions need your answers", 300, True, False),
])
def test_which_set_aside_listings_get_another_look(clean_db, status, reason, hours, application, again):
    jid = _set_aside("a-1", status, reason, hours_ago=hours, with_application=application)
    with session_scope() as s:
        assert _worth_another_look(s.get(Job, jid)) is again


async def test_a_listing_that_could_not_be_read_is_read_again_when_it_is_listed_again(orch):
    """LinkedIn job 4477577297 (run #42): its page timed out three times while searching and the job was recorded
    as Failed - for good, because every later search treated it as seen."""
    from app.profile import get_profile_store

    orch.profile = get_profile_store().load(force=True)
    unread = _set_aside("a-1", JobStatus.FAILED, "Error: NavigationFailed: Could not load the page: Timeout 30000ms exceeded.", hours_ago=30)
    paused = _set_aside("a-2", JobStatus.EXPIRED, "LinkedIn: Not currently accepting applications", hours_ago=30)
    recent = _set_aside("a-3", JobStatus.FAILED, "Error: NavigationFailed: Timeout", hours_ago=1)
    orch.portal.display_name, orch.portal.name = "LinkedIn", "linkedin"

    await orch.handle_posting(orch.portal, _posting("a-1", "linkedin", details_loaded=True))
    await orch.handle_posting(orch.portal, _posting("a-2", "linkedin", details_loaded=True,
                                                    extra={"expired": True, "expired_reason": "Not currently accepting applications"}))
    await orch.handle_posting(orch.portal, _posting("a-3", "linkedin", details_loaded=True))
    with session_scope() as s:
        got = {name: (s.get(Job, i).status, s.get(Job, i).status_reason) for name, i in
               (("unread", unread), ("paused", paused), ("recent", recent))}
    assert got["unread"][0] == JobStatus.QUEUED and got["unread"][1].startswith("Relevant")  # readable now: evaluated
    assert got["paused"] == (JobStatus.EXPIRED, "LinkedIn: Not currently accepting applications")  # still paused: says so
    assert got["recent"][0] == JobStatus.FAILED  # looked at an hour ago: not again yet
    assert len(_events("another_look")) == 2 and orch.stats["read_again"] == 2 and orch.stats["seen_before"] == 1


# ------------------------------------------------------------------ which service is answering

def test_health_says_which_service_is_answering(clean_db):
    from app.config import SERVICE_NAME
    from app.main import create_app

    with TestClient(create_app()) as c:
        body = c.get("/api/health").json()
    assert body == {"ok": True, "service": SERVICE_NAME} and SERVICE_NAME == "personal-job-assistant"


DOCKER = ("com.docker.backend", 55302, "*:8000")
OURS = ("Python", 4242, "127.0.0.1:8000")


@pytest.mark.parametrize("who,listeners,level,must_say", [
    ("ours", [OURS], "OK", ["the Job Assistant API is answering", "nothing else listens"]),
    ("ours", [OURS, DOCKER], "WARN", ["is shared", "com.docker.backend (pid 55302, *:8000)", "set PJA_API_PORT"]),
    ("other", [DOCKER], "WARN", ["answered by another program", "com.docker.backend", "set PJA_API_PORT", "make dev"]),
    ("none", [DOCKER], "WARN", ["the API is not running", "com.docker.backend", "set PJA_API_PORT"]),
    ("none", [], "INFO", ["the API is not running", "make dev"]),
    ("other", [], "WARN", ["answered by another program"]),  # (the port could not be listed: lsof missing)
])
def test_check_reports_a_port_shared_with_another_program(monkeypatch, who, listeners, level, must_say):
    monkeypatch.setattr(cli, "who_answers", lambda port: who)
    monkeypatch.setattr(cli, "port_listeners", lambda port: list(listeners))
    got_level, said = cli.api_port_report(8000)
    assert got_level == level and all(part in said for part in must_say), said
    assert "8000" in said


def test_who_answers_recognises_this_api_by_its_name_only(monkeypatch):
    import io
    import urllib.error
    import urllib.request

    answers = {"body": b'{"ok": true, "service": "personal-job-assistant"}'}

    class _Reply(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(url, timeout=3):
        assert url == "http://127.0.0.1:8000/api/health"
        if isinstance(answers["body"], Exception):
            raise answers["body"]
        return _Reply(answers["body"])

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert cli.who_answers(8000) == "ours"
    for foreign in (b'{"ok": true}', b'{"status": "healthy", "service": "other"}', b"<html>hi</html>", b"[1, 2]"):
        answers["body"] = foreign
        assert cli.who_answers(8000) == "other", foreign
    answers["body"] = urllib.error.HTTPError("http://127.0.0.1:8000/api/health", 404, "Not Found", {}, None)
    assert cli.who_answers(8000) == "other"
    answers["body"] = ConnectionRefusedError()
    assert cli.who_answers(8000) == "none"
