"""Job recency: only jobs posted within the window you chose are searched, queued and applied to.

Background (2026-10-14): Naukri ignored the "posted within 2 days" filter of its search (it only knows 1, 3, 7, 15 and
30 days) and nothing checked a job's age afterwards. Half of the Naukri listings were "3+ weeks ago", 27 of 58 Naukri
applications went to such jobs, and one approved application went to a job its own page dated 2026-06-25 - 111 days
earlier, after its listing had expired.

What is checked here:
* posting dates are read from the portal's own date or a relative label - never from when a job was found;
* old, recent, unknown, malformed, closed and open-ended ("3+ weeks ago") postings, time zones and day boundaries;
* the window is applied at discovery, in the queue (a job queued weeks ago does not slip through), in re-evaluation,
  and in the last check before the submit click - where the page's own date wins over the recorded one;
* the dashboard's choice is validated, saved and used by the job list.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.db import session_scope
from app.engine import recency as recency_mod
from app.engine.context import SubmitDecision
from app.engine.control import AgentControl
from app.engine.orchestrator import Orchestrator
from app.engine.recency import adjust, final_check, strictest
from app.engine.recorder import set_status, upsert_job
from app.matching import posting as P
from app.matching.posting import Posted, Window, parse_date, parse_label, verdict, window_from, window_to_dict
from app.models import AgentMode, AgentState, ApplicationEvent, Decision, Job, JobStatus, utcnow
from app.portals.base import SearchQuery
from app.portals.types import ApplyOutcome, ApplyResult, JobPosting

NOW = datetime(2026, 10, 14, 6, 0)  # naive UTC: 11:30 in India
IST = ZoneInfo("Asia/Kolkata")

FRONTEND_JD = ("We are hiring a frontend engineer.\nRequirements:\n- React, TypeScript, Redux, HTML, CSS\n- Next.js\n"
               "- Jest and React Testing Library\n- REST APIs, performance tuning, accessibility\n" * 2)


@pytest.fixture
def india(monkeypatch):
    """Calendar days counted in India (as on the machine this runs on), whatever zone the test machine is in."""
    monkeypatch.setattr(P, "local_tz", lambda: IST)


def _posting(ext: str, portal: str = "naukri", **kw) -> JobPosting:
    return JobPosting(portal=portal, external_id=ext, url=f"https://{portal}.test/{ext}", title="Senior Frontend Engineer",
                      company=f"Co {ext}", location="Bengaluru", experience_text="5-8 Yrs", description=FRONTEND_JD,
                      details_loaded=True, **kw)


def _window(stored: dict) -> None:
    with session_scope() as s:
        AgentState.get(s).recency = stored


# ================================================================== reading a posting date

@pytest.mark.parametrize("label,days,open_ended", [
    ("2 hours ago", 2 / 24, False),
    ("1 day ago", 1, False),
    ("Posted: 3 days ago", 3, False),
    ("Reposted 2 weeks ago", 14, False),
    ("3+ weeks ago", 21, True),  # a lower bound: at least 21 days, possibly months
    ("30+ days ago", 30, True),
    ("an hour ago", 1 / 24, False),
    ("a month ago", 30, False),
    ("Just now", 0, False),
    ("Few hours ago", 0, False),
    ("Today", 0, False),
    ("Yesterday", 1, False),
])
def test_a_relative_label_is_counted_from_when_it_was_read(label, days, open_ended):
    p = parse_label(label, seen=NOW)
    assert p is not None and p.source == "label" and p.open_ended is open_ended
    assert abs((NOW - p.at).total_seconds() - days * 86400) < 1


@pytest.mark.parametrize("label", ["", "   ", "Posted recently", "ago", "Hiring", "99999 days ago", "Actively recruiting",
                                   "3 applicants", "Be an early applicant"])
def test_a_label_without_a_date_is_unknown_never_guessed(label):
    assert parse_label(label, seen=NOW) is None


@pytest.mark.parametrize("value,expected,date_only", [
    ("2026-06-25", date(2026, 6, 25), True),
    ("2026-06-25T08:30:00+05:30", datetime(2026, 6, 25, 3, 0), False),
    ("2026-06-25T08:30:00Z", datetime(2026, 6, 25, 8, 30), False),
    ("25 Jun 2026", date(2026, 6, 25), True),
    ("Jun 25, 2026", date(2026, 6, 25), True),
    ("25th June 2026", date(2026, 6, 25), True),
    ("25/06/2026", date(2026, 6, 25), True),
    ("06/25/2026", date(2026, 6, 25), True),
])
def test_an_absolute_date_is_the_portals_own(india, value, expected, date_only):
    p = parse_date(value, now=NOW)
    assert p is not None and p.source == "page" and p.date_only is date_only
    assert (p.local_date() if date_only else p.at) == expected


@pytest.mark.parametrize("value", [
    "05/06/2026",  # May or June: not reliable
    "2026-13-45", "31 Feb 2026", "garbage", "Posted on", "1999-01-01",
    "2026-12-01",  # in the future: not believed
])
def test_a_malformed_ambiguous_or_future_date_is_not_believed(india, value):
    assert parse_date(value, now=NOW) is None


def test_a_calendar_date_stays_on_its_day_in_any_time_zone(monkeypatch):
    """Stored at noon in this machine's zone: a day boundary elsewhere never moves it to another date."""
    for zone in ("Asia/Kolkata", "America/Los_Angeles", "Pacific/Kiritimati", "UTC"):
        monkeypatch.setattr(P, "local_tz", lambda z=zone: ZoneInfo(z))
        p = parse_date("2026-10-01", now=NOW)
        assert p is not None and p.local_date() == date(2026, 10, 1), zone


def test_today_in_india_is_not_tomorrow_in_utc(india):
    """At 01:30 in India it is still yesterday in UTC: a job dated today there is today, not 'in the future'."""
    late = datetime(2026, 10, 13, 20, 0)  # 2026-10-14 01:30 IST
    p = parse_date("2026-10-14", now=late)
    assert p is not None and P.age_days(p, now=late) == 0
    assert verdict(p, Window(days=1), now=late)[0] == "in"


# ================================================================== the window

@pytest.mark.parametrize("stored,expected", [
    (None, Window(days=7)),
    ({}, Window(days=7)),  # nothing chosen yet: the last 7 days
    ({"days": 30}, Window(days=30)),
    ({"all": True}, Window(days=None)),
    ({"start": "2026-09-01", "end": None}, Window(days=None, start=date(2026, 9, 1))),
    ({"start": "2026-09-01", "end": "2026-09-30"}, Window(days=None, start=date(2026, 9, 1), end=date(2026, 9, 30))),
    ({"start": "garbage"}, Window(days=7)),  # a damaged value: the safe default, never "all dates"
    ({"days": "x"}, Window(days=7)),
    ({"days": 0}, Window(days=7)),
])
def test_the_stored_window(stored, expected):
    assert window_from(stored) == expected
    if stored and window_from(stored) != Window(days=7):
        assert window_from(window_to_dict(expected)) == expected


@pytest.mark.parametrize("posted,window,state", [
    (Posted(NOW - timedelta(days=6, hours=23), source="label"), Window(days=7), "in"),
    (Posted(NOW - timedelta(days=7), source="label"), Window(days=7), "in"),  # the boundary itself
    (Posted(NOW - timedelta(days=7, minutes=1), source="label"), Window(days=7), "out"),
    (Posted(NOW - timedelta(hours=23), source="label"), Window(days=1), "in"),
    (Posted(NOW - timedelta(hours=25), source="label"), Window(days=1), "out"),
    (Posted(NOW - timedelta(days=111), source="page"), Window(days=90), "out"),
    (Posted(NOW - timedelta(days=21), open_ended=True, source="label"), Window(days=7), "out"),
    (Posted(NOW - timedelta(days=21), open_ended=True, source="label"), Window(days=30), "unknown"),  # maybe months
    (None, Window(days=7), "unknown"),
    (None, Window(days=None), "in"),  # "All dates" - only when you chose it
    (Posted(NOW - timedelta(days=400), source="page"), Window(days=None), "in"),
])
def test_verdict_by_age(india, posted, window, state):
    assert verdict(posted, window, now=NOW)[0] == state


@pytest.mark.parametrize("age,state", [(0, "in"), (7, "in"), (8, "out")])
@pytest.mark.parametrize("clock", [time(0, 5), time(12, 0), time(23, 55)])
def test_a_calendar_date_counts_calendar_days(india, age, state, clock):
    """'The last 7 days' are today and the 7 days before it - at any time of day."""
    today = date(2026, 10, 14)
    now = datetime.combine(today, clock, tzinfo=IST).astimezone(UTC).replace(tzinfo=None)
    posted = parse_date((today - timedelta(days=age)).isoformat(), now=now)
    assert posted is not None and P.age_days(posted, now=now) == age
    assert verdict(posted, Window(days=7), now=now)[0] == state


@pytest.mark.parametrize("posted,state", [
    (Posted(datetime(2026, 9, 10, 6), source="page"), "in"),
    (Posted(datetime(2026, 8, 31, 6), source="page"), "out"),  # before the range
    (Posted(datetime(2026, 10, 1, 6), source="page"), "out"),  # after it
    (Posted(datetime(2026, 9, 10, 6), open_ended=True, source="label"), "unknown"),  # may be older than the start
])
def test_verdict_for_a_custom_range(india, posted, state):
    window = Window(days=None, start=date(2026, 9, 1), end=date(2026, 9, 30))
    assert verdict(posted, window, now=NOW)[0] == state


@pytest.mark.parametrize("label,said", [("8 minutes ago", "less than an hour ago"), ("1 hour ago", "1 hour ago"),
                                        ("5 hours ago", "5 hours ago"), ("1 day ago", "1 day ago"), ("3 days ago", "3 days ago")])
def test_an_age_reads_naturally(label, said):
    assert f"({said}, from the listing's label)" in P.describe(parse_label(label, seen=NOW), now=NOW)


def test_a_listing_past_its_own_end_date_is_closed_whatever_its_age():
    job = SimpleNamespace(valid_through=utcnow() - timedelta(days=1))
    assert "end date" in P.listing_closed(job)
    assert P.listing_closed(SimpleNamespace(valid_through=utcnow() + timedelta(days=30))) == ""
    assert P.listing_closed(SimpleNamespace()) == ""


def test_the_strictest_window_decides():
    old = Posted(utcnow() - timedelta(days=20), source="label")
    state, _, window = strictest(old, [Window(days=None), Window(days=7)])
    assert state == "out" and window == Window(days=7)
    assert strictest(None, [Window(days=None), Window(days=30)])[0] == "unknown"
    assert strictest(old, [Window(days=30)])[0] == "in"


# ================================================================== the record

def test_the_day_a_job_was_found_is_never_its_posting_date(clean_db):
    with session_scope() as s:
        job, _ = upsert_job(s, _posting("u-1"))
        job.discovered_at = utcnow() - timedelta(days=200)
        assert job.posted_at is None and P.posted_of(job) is None
        assert verdict(P.posted_of(job), Window(days=7))[0] == "unknown"


def test_the_portals_own_date_beats_a_label_and_a_repeated_label_does_not_move_it(clean_db):
    with session_scope() as s:
        job, _ = upsert_job(s, _posting("r-1", posted_text="2 days ago"))
        first = job.posted_at
        assert first is not None and job.posted_source == "label"
        # the same label read again a moment later: the same date - the record is not touched
        upsert_job(s, _posting("r-1", posted_text="2 days ago"))
        assert job.posted_at == first
        # an open-ended label is weaker evidence than a precise one: it does not replace it
        upsert_job(s, _posting("r-1", posted_text="3+ weeks ago"))
        assert job.posted_at == first and not job.posted_open_ended
        # a clearly earlier label of the same kind does (an estimate errs towards older)
        upsert_job(s, _posting("r-1", posted_text="5 days ago"))
        assert job.posted_at < first - timedelta(days=2)
        # the page's own date wins, and a later label never overrides it
        page_date = utcnow() - timedelta(days=111)
        upsert_job(s, _posting("r-1", posted_text="1 day ago", posted_at=page_date, posted_date_only=True))
        assert job.posted_source == "page" and job.posted_at == page_date and not job.posted_open_ended
        upsert_job(s, _posting("r-1", posted_text="Just now"))
        assert job.posted_source == "page" and job.posted_at == page_date


def test_old_records_get_their_posting_date_from_their_label_once(clean_db):
    from app.db import _backfill_posted_dates, get_engine

    with session_scope() as s:
        job, _ = upsert_job(s, _posting("b-1", posted_text="3+ weeks ago"))
        other, _ = upsert_job(s, _posting("b-2", posted_text="Actively hiring"))
        s.flush()
        ids = job.id, other.id
    found = datetime(2026, 9, 1, 6)
    with get_engine().begin() as conn:  # as a record from before posting dates were kept
        conn.execute(text("UPDATE jobs SET posted_at = NULL, posted_source = '', discovered_at = :at"), {"at": found})
    _backfill_posted_dates(get_engine())
    with session_scope() as s:
        job, other = s.get(Job, ids[0]), s.get(Job, ids[1])
        assert job.posted_at == found - timedelta(days=21) and job.posted_open_ended and job.posted_source == "label"
        assert other.posted_at is None  # no date in its label: still unknown


# ================================================================== discovery

class _Engine:
    alive = False
    user_data_dir = "/nonexistent"

    async def start(self):
        return None


class _Portal:
    name = "naukri"
    display_name = "Naukri"

    async def ensure_logged_in(self, gate):
        return None

    async def search(self, q):
        return
        yield

    async def fetch_details(self, posting):
        return posting


@pytest.fixture
def orch(clean_db, profile):
    with session_scope() as s:
        st = AgentState.get(s)
        st.mode, st.portals, st.desired_state = "auto", ["naukri"], "RUNNING"
    portal = _Portal()
    o = Orchestrator(_Engine(), AgentControl(cache_s=0), adapter_factory=lambda n, e: portal)
    o.portal = portal
    o.profile = profile
    o.mode = AgentMode.AUTO
    o.window = Window(days=7)
    return o


def _status(ext: str) -> tuple[str, str]:
    with session_scope() as s:
        job = s.scalar(select(Job).where(Job.external_id == ext))
        return job.status, job.status_reason


async def test_discovery_skips_an_old_job_without_evaluating_it(orch, monkeypatch):
    class NoMatcher:
        def __init__(self, *a, **k):
            raise AssertionError("an old job is not evaluated")

    monkeypatch.setattr("app.engine.orchestrator.JobMatcher", NoMatcher)
    await orch.handle_posting(orch.portal, _posting("d-old", posted_text="3+ weeks ago"))
    status, reason = _status("d-old")
    assert status == JobStatus.SKIPPED and "outside your job-recency window" in reason and "at least 21 days" in reason
    with session_scope() as s:
        job = s.scalar(select(Job).where(Job.external_id == "d-old"))
        assert job.match_details.get("recency_skip") == {"days": 7}


async def test_discovery_closes_a_job_past_its_end_date(orch):
    await orch.handle_posting(orch.portal, _posting("d-closed", posted_text="1 day ago",
                                                    valid_through=utcnow() - timedelta(days=2)))
    status, reason = _status("d-closed")
    assert status == JobStatus.EXPIRED and reason.startswith("Closed:")


async def test_discovery_queues_a_recent_job_and_leaves_an_undated_one_to_you(orch):
    await orch.handle_posting(orch.portal, _posting("d-new", posted_text="1 day ago"))
    await orch.handle_posting(orch.portal, _posting("d-undated"))
    assert _status("d-new")[0] == JobStatus.QUEUED
    status, reason = _status("d-undated")
    assert status == JobStatus.REVIEW_PENDING and "posting date unknown" in reason


# ================================================================== the queue: queued long ago, applied to now?

def _queued(ext: str, **kw) -> int:
    with session_scope() as s:
        job, _ = upsert_job(s, _posting(ext, **kw))
        job.match_score = 90.0
        set_status(s, job, JobStatus.QUEUED, "Relevant (score 90)")
        s.flush()
        return job.id


async def test_a_job_queued_under_an_older_window_is_not_applied_to(orch, monkeypatch):
    old = _queued("q-old", posted_at=utcnow() - timedelta(days=40))
    fresh = _queued("q-fresh", posted_text="2 days ago")
    _window({"days": 7})
    applied: list[int] = []

    async def apply_job(job_id, adapter, posting):
        applied.append(job_id)
        with session_scope() as s:
            set_status(s, s.get(Job, job_id), JobStatus.APPLIED, "Applied (test)")
        return ApplyResult(ApplyOutcome.APPLIED, "Applied (test)", submit_attempted=True)

    monkeypatch.setattr(orch, "apply_job", apply_job)
    orch.only_jobs = {old, fresh}
    summary = await orch.run()
    assert summary["status"] == "COMPLETED", summary
    assert applied == [fresh]
    with session_scope() as s:
        job = s.get(Job, old)
        assert job.status == JobStatus.SKIPPED and job.status_reason.startswith("Not applied: Posted")
        assert job.match_details["recency_skip"] == {"days": 7}  # so a wider window looks at it again
        said = [e.message for e in s.scalars(select(ApplicationEvent).where(ApplicationEvent.job_id == old))]
    assert any("outside your job-recency window" in m for m in said)


async def test_an_undated_queued_job_waits_for_you_in_auto_apply(orch):
    jid = _queued("q-undated")
    with session_scope() as s:
        assert orch._recency_gate(s, s.get(Job, jid)) is True
    assert _status("q-undated")[0] == JobStatus.REVIEW_PENDING


async def test_an_undated_queued_job_goes_on_to_your_approval_in_review_mode(orch):
    jid = _queued("q-undated-review")
    orch.mode = AgentMode.REVIEW
    with session_scope() as s:
        assert orch._recency_gate(s, s.get(Job, jid)) is False  # you see it before anything is sent


async def test_a_window_narrowed_during_a_run_applies_at_once(orch):
    jid = _queued("q-mid", posted_at=utcnow() - timedelta(days=20))
    orch.window = Window(days=None)  # the run started with "All dates"
    _window({"days": 7})  # ...and you chose the last 7 days since
    with session_scope() as s:
        assert orch._recency_gate(s, s.get(Job, jid)) is True
    assert _status("q-mid")[0] == JobStatus.SKIPPED


async def test_a_closed_queued_job_is_expired(orch):
    jid = _queued("q-closed", posted_text="1 day ago", valid_through=utcnow() - timedelta(hours=1))
    with session_scope() as s:
        assert orch._recency_gate(s, s.get(Job, jid)) is True
    assert _status("q-closed")[0] == JobStatus.EXPIRED


# ================================================================== right before the submit click

class _Page:
    def __init__(self, found: dict | None = None, broken: bool = False):
        self.found, self.broken = found or {}, broken

    async def evaluate(self, js, *args):
        if self.broken:
            raise RuntimeError("page gone")
        return self.found


def _record(**kw):
    base = dict(posted_at=None, posted_open_ended=False, posted_date_only=False, posted_source="", posted_text="",
                discovered_at=utcnow(), valid_through=None)
    return SimpleNamespace(**{**base, **kw})


async def test_the_pages_own_date_wins_right_before_the_click():
    """Recorded as '1 day ago' by a listing; the job page says it was posted 111 days ago: not sent."""
    record = _record(posted_at=utcnow() - timedelta(days=1), posted_source="label")
    old_day = (utcnow() - timedelta(days=111)).date().isoformat()
    why = await final_check(record, Window(days=7), _Page({"datePosted": old_day}), unknown_blocks=True)
    assert "outside your job-recency window" in why and "the portal's date" in why
    assert await final_check(record, Window(days=7), _Page(), unknown_blocks=True) == ""  # no page date: the record
    assert await final_check(record, Window(days=7), _Page(broken=True), unknown_blocks=True) == ""


async def test_a_listing_closed_by_its_page_is_not_sent():
    record = _record(posted_at=utcnow() - timedelta(days=1), posted_source="label")
    gone = (utcnow() - timedelta(days=3)).date().isoformat()
    why = await final_check(record, Window(days=7), _Page({"validThrough": gone}), unknown_blocks=True)
    assert why.startswith("the job is closed")


async def test_the_last_check_says_what_it_checked_when_it_lets_a_job_through():
    heard: list[str] = []
    record = _record(posted_at=utcnow() - timedelta(days=1), posted_source="label", posted_spread_s=86400)
    today = datetime.now(P.local_tz()).date().isoformat()
    assert await final_check(record, Window(days=7), _Page({"datePosted": today}), unknown_blocks=True,
                             on_pass=heard.append) == ""
    assert len(heard) == 1 and "the portal's date" in heard[0] and "within" in heard[0]
    heard.clear()
    assert await final_check(_record(), Window(days=7), None, unknown_blocks=True, on_pass=heard.append)
    assert heard == []  # stopped: nothing to confirm
    assert await final_check(_record(), Window(days=7), None, unknown_blocks=False, on_pass=heard.append) == ""
    assert "you approve it before it is sent" in heard[0]  # Review Mode: you see it first


async def test_the_run_records_the_last_check_in_the_jobs_timeline(orch):
    jid = _queued("f-1", posted_text="1 day ago")
    check = orch._final_recency_check(jid)
    assert await check(None) == ""
    with session_scope() as s:
        said = [e.message for e in s.scalars(select(ApplicationEvent).where(ApplicationEvent.job_id == jid,
                                                                            ApplicationEvent.type == "recency_check"))]
    assert len(said) == 1 and said[0].startswith("Last check before submitting: posted on")


async def test_the_pages_own_dates_read_at_the_last_check_are_kept(orch):
    """Recorded from a listing label ("2 days ago" - precise to a day); the job page states its date: the record keeps
    the portal's date from then on (the candidate list and the dashboard show it)."""
    jid = _queued("f-2", posted_text="2 days ago")
    day = (datetime.now(P.local_tz()).date() - timedelta(days=3)).isoformat()
    end = (datetime.now(P.local_tz()).date() + timedelta(days=60)).isoformat()
    check = orch._final_recency_check(jid)
    assert await check(_Page({"datePosted": day, "validThrough": end})) == ""
    with session_scope() as s:
        job = s.get(Job, jid)
        assert job.posted_source == "page" and job.posted_date_only and job.posted_spread_s == 0
        assert P.posted_of(job).local_date().isoformat() == day and job.valid_through is not None
    assert await check(_Page()) == ""  # a page without dates changes nothing
    with session_scope() as s:
        assert s.get(Job, jid).posted_source == "page"


async def test_an_unknown_date_blocks_only_auto_apply():
    assert await final_check(_record(), Window(days=7), None, unknown_blocks=True)
    assert await final_check(_record(), Window(days=7), None, unknown_blocks=False) == ""
    assert await final_check(_record(), [Window(days=None)], None, unknown_blocks=True) == ""


async def test_the_central_submit_decision_honours_the_last_check(profile):
    from tests.helpers import make_ctx

    for mode, expected in ((AgentMode.AUTO, SubmitDecision.MANUAL), (AgentMode.REVIEW, SubmitDecision.MANUAL),
                           (AgentMode.DRY_RUN, SubmitDecision.DRY_RUN)):
        ctx = make_ctx(None, profile, _posting("s-1"), mode=mode)

        async def too_old(page):
            return "posted on 2026-06-25 (111 days ago, the portal's date): outside your job-recency window"

        ctx.recency_check = too_old
        assert await ctx.decide_submit(None, unresolved=[]) == expected
        assert ctx.recency_blocked.startswith("posted on 2026-06-25") and ctx.submits == []
        assert any(kind == "recency" for kind, _ in ctx.events)


# ================================================================== re-evaluation and a wider window

def test_a_score_never_brings_an_old_job_back():
    old = _record(posted_at=utcnow() - timedelta(days=40), posted_source="page")
    decision, why, state = adjust(Decision.APPLY, "Relevant (score 95)", old, Window(days=30))
    assert decision == Decision.SKIP and state == "out" and why.startswith("Posted on")
    decision, why, state = adjust(Decision.APPLY, "Relevant (score 95)", _record(), Window(days=30))
    assert decision == Decision.MANUAL_REVIEW and "posting date unknown" in why
    assert adjust(Decision.SKIP, "Not frontend", _record(), Window(days=30))[0] == Decision.SKIP
    closed = _record(posted_at=utcnow(), valid_through=utcnow() - timedelta(days=1))
    assert adjust(Decision.APPLY, "x", closed, Window(days=None))[1].startswith("Closed:")


def test_jobs_skipped_for_their_age_are_looked_at_again_when_the_window_widens(clean_db, profile):
    from app.engine.reconsider import release_recency

    with session_scope() as s:
        aged, _ = upsert_job(s, _posting("w-20", posted_at=utcnow() - timedelta(days=20)))
        ancient, _ = upsert_job(s, _posting("w-200", posted_at=utcnow() - timedelta(days=200)))
        other, _ = upsert_job(s, _posting("w-not-frontend", posted_at=utcnow() - timedelta(days=20)))
        for job in (aged, ancient):
            job.match_details = {"recency_skip": {"days": 7}}
            set_status(s, job, JobStatus.SKIPPED, "Posted ... outside your job-recency window")
        set_status(s, other, JobStatus.SKIPPED, "Not a frontend role")  # skipped for another reason: untouched
    preview: list[dict] = []
    assert release_recency(profile, Window(days=30), dry=True, changes=preview) == {"queued": 1, "unchanged": 1}
    assert [row["company"] for row in preview] == ["Co w-20"] and _status("w-20")[0] == JobStatus.SKIPPED  # dry
    release_recency(profile, Window(days=30))
    assert _status("w-20")[0] == JobStatus.QUEUED
    assert _status("w-200")[0] == JobStatus.SKIPPED and _status("w-not-frontend")[0] == JobStatus.SKIPPED


# ================================================================== the portals' own search filters

@pytest.mark.parametrize("days,age", [(None, None), (1, 1), (2, 3), (3, 3), (7, 7), (14, 15), (30, 30), (60, None)])
def test_naukri_is_asked_only_for_a_freshness_it_knows(days, age):
    from app.portals.naukri import NaukriAdapter

    assert NaukriAdapter.job_age(days) == age
    url = NaukriAdapter(None, "https://naukri.test").search_url(SearchQuery("React", "Pune", posted_within_days=days), 1)
    assert (f"jobAge={age}" in url) if age else ("jobAge" not in url)


def test_a_custom_range_searches_back_to_its_first_day(india):
    start = datetime.now(IST).date() - timedelta(days=9)
    assert Window(days=None, start=start).search_days() == 10
    assert Window(days=14).search_days() == 14 and Window(days=None).search_days() is None


# ================================================================== the dashboard's choice

@pytest.fixture
def client(clean_db):
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


def test_the_default_is_the_last_seven_days(client):
    _window({})
    r = client.get("/api/agent").json()["recency"]
    assert r["days"] == 7 and r["chosen"] is False and r["label"] == "posted in the last 7 days"
    assert r["choices"] == [1, 3, 7, 14, 30, 60, 90]
    assert recency_mod.current_window() == Window(days=7)


@pytest.mark.parametrize("body,stored", [
    ({"days": 1}, {"days": 1}),
    ({"days": 90}, {"days": 90}),
    ({"all": True}, {"all": True}),
    ({"start": "2026-09-01"}, {"start": "2026-09-01", "end": None}),
])
def test_a_choice_is_saved_and_used_by_every_later_run(client, body, stored, monkeypatch):
    monkeypatch.setattr("app.api.routes_agent.datetime", _FixedNow)
    r = client.put("/api/agent/recency", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["recency"]["chosen"] is True
    with session_scope() as s:
        assert AgentState.get(s).recency == stored
        assert any(e.type == "recency" for e in s.scalars(select(ApplicationEvent)))
    assert client.get("/api/agent").json()["recency"]["label"] == window_from(stored).label()  # after a reload


class _FixedNow(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 10, 14, 11, 30, tzinfo=tz)


@pytest.mark.parametrize("body", [
    {}, {"days": 5}, {"days": 0}, {"days": 365}, {"days": 7, "all": True}, {"days": 7, "start": "2026-09-01"},
    {"all": False}, {"end": "2026-10-01"}, {"start": "2026-10-10", "end": "2026-10-01"}, {"start": "2026-10-20"},
    {"start": "2025-01-01", "end": "2025-02-01"}, {"start": "not a date"}, {"days": "7; DROP TABLE jobs"},
    {"days": 7, "token": "x"},
])
def test_an_invalid_choice_is_refused_and_nothing_changes(client, body, monkeypatch):
    monkeypatch.setattr("app.api.routes_agent.datetime", _FixedNow)
    _window({"days": 14})
    r = client.put("/api/agent/recency", json=body)
    assert r.status_code == 422, (body, r.text)
    with session_scope() as s:
        assert AgentState.get(s).recency == {"days": 14}


def test_the_job_list_follows_the_window(client, india):
    with session_scope() as s:
        for ext, kw in [("l-new", {"posted_text": "1 day ago"}), ("l-old", {"posted_text": "3+ weeks ago"}),
                        ("l-undated", {}), ("l-page-old", {"posted_at": utcnow() - timedelta(days=111), "posted_date_only": True}),
                        ("l-edge", {"posted_at": P._local_noon_utc(datetime.now(IST).date() - timedelta(days=7)),
                                    "posted_date_only": True})]:
            upsert_job(s, _posting(ext, **kw))
    _window({"days": 7})

    def listed(**params) -> tuple[dict[str, dict], dict]:
        body = client.get("/api/jobs", params={"page_size": 50, **params}).json()
        return {j["company"].removeprefix("Co "): j for j in body["items"]}, body["recency"]

    jobs, info = listed(recency="true")
    assert set(jobs) == {"l-new", "l-undated", "l-edge"} and info["applied"] is True and info["days"] == 7
    jobs, _ = listed(recency="true", include_undated="false")
    assert set(jobs) == {"l-new", "l-edge"}
    jobs, info = listed()  # history: every posting date
    assert set(jobs) == {"l-new", "l-old", "l-undated", "l-page-old", "l-edge"} and info["applied"] is False
    assert jobs["l-old"]["recency"] == "out" and jobs["l-old"]["posted_open_ended"] is True
    assert jobs["l-undated"]["recency"] == "unknown" and jobs["l-undated"]["posted_at"] is None
    assert jobs["l-new"]["recency"] == "in" and jobs["l-new"]["posted_label"].startswith("on ")
    assert jobs["l-page-old"]["posted_source"] == "page" and jobs["l-page-old"]["posted_age_days"] == 111
    assert jobs["l-edge"]["posted_age_days"] == 7 and jobs["l-edge"]["recency"] == "in"


def test_a_job_page_says_when_it_was_posted_and_why_it_is_not_applied_to(client):
    with session_scope() as s:
        job, _ = upsert_job(s, _posting("p-1", posted_text="3+ weeks ago", valid_through=utcnow() - timedelta(days=1)))
        s.flush()
        jid = job.id
    _window({"days": 7})
    d = client.get(f"/api/jobs/{jid}").json()
    assert d["posted_label"].startswith("at least 21 days ago") and d["posted_text"] == "3+ weeks ago"
    assert d["recency"] == "out" and "end date" in d["closed_reason"]


# ================================================================== a label is precise only to its unit

@pytest.mark.parametrize("label,spread", [
    ("2 hours ago", 3600), ("1 day ago", 86400), ("Posted: 3 days ago", 86400), ("1 week ago", 7 * 86400),
    ("Reposted 2 weeks ago", 7 * 86400), ("a month ago", 30 * 86400), ("Just now", 3600), ("Few hours ago", 86400),
    ("Today", 86400), ("Yesterday", 86400), ("25 Jun 2026", 0),
])
def test_a_label_keeps_its_precision(label, spread):
    p = parse_label(label, seen=NOW)
    assert p is not None and p.spread_s == spread and P.label_spread(label) == spread


@pytest.mark.parametrize("label,read_days_ago,window,state", [
    # "1 week ago" means 7 to 13 days: never surely within the last 7 days, surely within 14
    ("1 week ago", 0, 7, "unknown"),
    ("1 week ago", 0, 14, "in"),
    ("1 week ago", 1, 14, "unknown"),
    # "2 days ago" read 5 days ago: 7 to 8 days - maybe just outside the last 7 days
    ("2 days ago", 5, 7, "unknown"),
    ("2 days ago", 4, 7, "in"),
    ("2 days ago", 6, 7, "out"),
    # "1 day ago" is 24 to 47 hours: not surely within the last 24 hours
    ("1 day ago", 0, 1, "unknown"),
    ("2 hours ago", 0, 1, "in"),
    ("Just now", 0, 1, "in"),
])
def test_a_posting_that_may_lie_just_outside_the_window_is_yours_to_decide(label, read_days_ago, window, state):
    seen = NOW - timedelta(days=read_days_ago)
    state_now, why = verdict(parse_label(label, seen=seen), Window(days=window), now=NOW)
    assert state_now == state, why
    if state == "unknown":
        assert "precise only to" in why


def test_a_custom_range_with_an_imprecise_label(india):
    window = Window(days=None, start=date(2026, 10, 1), end=date(2026, 10, 10))
    # read on 10-15 as "1 week ago": 7 to 13 days before - 10-02 to 10-08 - inside
    seen = datetime(2026, 10, 15, 6)
    assert verdict(parse_label("1 week ago", seen=seen), window, now=seen)[0] == "in"
    # read on 10-14 as "2 weeks ago": 09-24 to 09-30 - before it
    assert verdict(parse_label("2 weeks ago", seen=datetime(2026, 10, 14, 6)), window, now=NOW)[0] == "out"
    # read on 10-12 as "1 week ago": 09-29 to 10-05 - straddles its start
    assert verdict(parse_label("1 week ago", seen=datetime(2026, 10, 12, 6)), window, now=NOW)[0] == "unknown"
    # read on 10-18 as "1 week ago": 10-05 to 10-11 - straddles its end
    late = datetime(2026, 10, 18, 6)
    assert verdict(parse_label("1 week ago", seen=late), window, now=late)[0] == "unknown"


def test_the_record_keeps_the_precision_of_the_evidence_it_kept(clean_db):
    with session_scope() as s:
        job, _ = upsert_job(s, _posting("pr-1", posted_text="1 week ago"))
        assert job.posted_spread_s == 7 * 86400
        upsert_job(s, _posting("pr-1", posted_text="1 week ago"))  # read again: unchanged
        assert job.posted_spread_s == 7 * 86400
        page = utcnow() - timedelta(days=12)
        upsert_job(s, _posting("pr-1", posted_at=page, posted_date_only=True))  # the portal's own date: exact
        assert job.posted_source == "page" and job.posted_spread_s == 0
        assert P.posted_of(job).spread_s == 0


def test_dates_read_before_their_precision_was_kept_get_it_once(clean_db):
    from app.db import _backfill_posted_precision, get_engine

    with session_scope() as s:
        week, _ = upsert_job(s, _posting("pp-1", posted_text="1 week ago"))
        page, _ = upsert_job(s, _posting("pp-2", posted_at=utcnow() - timedelta(days=3)))
        s.flush()
        ids = week.id, page.id
    with get_engine().begin() as conn:
        conn.execute(text("UPDATE jobs SET posted_spread_s = NULL"))
    with session_scope() as s:  # still read conservatively before the backfill
        assert P.posted_of(s.get(Job, ids[0])).spread_s == 7 * 86400
    _backfill_posted_precision(get_engine())
    with session_scope() as s:
        assert s.get(Job, ids[0]).posted_spread_s == 7 * 86400
        assert s.get(Job, ids[1]).posted_spread_s is None  # the portal's date: nothing to add (read as exact)
        assert P.posted_of(s.get(Job, ids[1])).spread_s == 0


async def test_a_borderline_label_waits_for_you_instead_of_being_queued(orch):
    """"6 days ago" is 6 to 7 days: perhaps just outside the last 7 days - a relevant job waits for your decision."""
    await orch.handle_posting(orch.portal, _posting("d-border", posted_text="6 days ago"))
    status, reason = _status("d-border")
    assert status == JobStatus.REVIEW_PENDING and "precise only to a day" in reason


# ================================================================== the portal's own dates, read from a real page

_LD_PAGE = """<html><head><title>Frontend Engineer</title>
<script type="application/ld+json">{"@type": "Organization", "name": "Acme Labs", "datePosted": "2020-01-01"}</script>
<script type="application/ld+json">{"@context": "https://schema.org", "@type": "JobPosting", "title": "Frontend Engineer",
 "datePosted": "%s", "validThrough": "%s"}</script></head><body><h1>Frontend Engineer</h1></body></html>"""


@pytest.mark.browser
async def test_the_portals_own_dates_are_read_from_a_real_page(tmp_path):
    """Naukri, Instahyre and many company sites state the posting in schema.org JobPosting data; only that block is
    read (an organisation's block with a date of its own is not the job's)."""
    from app.browser.engine import BrowserEngine
    from app.portals.common import read_page_dates

    e = BrowserEngine(headless=True, user_data_dir=tmp_path / "p", screenshot_dir=tmp_path / "s", human_delays=False)
    await e.start()
    try:
        today = datetime.now(P.local_tz()).date()
        await e.page.set_content(_LD_PAGE % ("2026-06-25", (today + timedelta(days=30)).isoformat()))
        found = SimpleNamespace(posted_at=None, posted_date_only=False, valid_through=None)
        await read_page_dates(e.page, found)
        assert found.posted_date_only and Posted(found.posted_at).local_date() == date(2026, 6, 25)
        assert found.valid_through is not None
        recorded_fresh = _record(posted_at=utcnow() - timedelta(days=1), posted_source="label", posted_spread_s=86400)
        why = await final_check(recorded_fresh, Window(days=7), e.page, unknown_blocks=True)
        assert "outside your job-recency window" in why and "the portal's date" in why  # the page wins
        await e.page.set_content(_LD_PAGE % ((today - timedelta(days=2)).isoformat(), (today - timedelta(days=1)).isoformat()))
        assert (await final_check(recorded_fresh, Window(days=7), e.page, unknown_blocks=True)).startswith("the job is closed")
    finally:
        await e.stop()
