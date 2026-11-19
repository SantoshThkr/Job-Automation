"""Discovery that counts what it saw once, and does not repeat a search that keeps listing the same jobs.

Background (2026-10-15, run 76): 1 832 listings "discovered" were 1 832 sightings - 1 726 of them jobs already known,
many seen several times in the same run. Instahyre alone made 65 searches (5 queries x 13 cities) that listed the same
few dozen jobs: 1 300 sightings, one new job.
"""

from __future__ import annotations

import pytest

from app.db import session_scope
from app.engine.control import AgentControl
from app.engine.orchestrator import REDUNDANT_SEARCHES, Orchestrator
from app.models import AgentState
from app.portals.types import JobPosting

JD = ("We are hiring a frontend engineer.\nRequirements:\n- React, TypeScript, Redux, HTML, CSS\n- Next.js\n"
      "- Jest and React Testing Library\n- REST APIs, performance tuning, accessibility\n" * 2)


class _Engine:
    alive = False
    user_data_dir = "/nonexistent"

    async def start(self):
        return None


class _SamePortal:
    """Lists the same two jobs for whatever is asked (as Instahyre's feed did)."""
    name = "instahyre"
    display_name = "Instahyre"

    def __init__(self, listings=("a", "b")):
        self.listings, self.searched = listings, []

    async def ensure_logged_in(self, gate):
        return None

    async def search(self, q):
        self.searched.append(f"{q.keywords} / {q.location}")
        for ext in self.listings:
            yield JobPosting(portal="instahyre", external_id=ext, url=f"https://instahyre.test/{ext}",
                             title="Senior Frontend Engineer", company=f"Co {ext}", location="Bengaluru",
                             description=JD, posted_text="1 day ago", details_loaded=True)

    async def fetch_details(self, posting):
        return posting


class _NewEachTime(_SamePortal):
    """Lists different jobs for every search (as LinkedIn and Naukri do for different cities)."""

    async def search(self, q):
        self.searched.append(f"{q.keywords} / {q.location}")
        n = len(self.searched)
        for i in range(2):
            ext = f"{n}-{i}"
            yield JobPosting(portal="instahyre", external_id=ext, url=f"https://instahyre.test/{ext}",
                             title="Senior Frontend Engineer", company=f"Co {ext}", location="Bengaluru",
                             description=JD, posted_text="1 day ago", details_loaded=True)


def _orch(portal, profile):
    with session_scope() as s:
        st = AgentState.get(s)
        st.mode, st.portals, st.desired_state = "dry_run", ["instahyre"], "RUNNING"
    o = Orchestrator(_Engine(), AgentControl(cache_s=0), adapter_factory=lambda n, e: portal)
    o.profile = profile.model_copy(update={"search": profile.search.model_copy(update={
        "queries": ["react developer", "frontend developer", "next.js developer"],
        "locations": ["Bengaluru", "Pune", "Remote", "Noida"]})})
    return o


@pytest.mark.parametrize("portal_cls", [_SamePortal])
async def test_a_portal_that_repeats_itself_is_not_searched_on_and_on(clean_db, profile, portal_cls):
    portal = portal_cls()
    o = _orch(portal, profile)
    await o.discover_portal("instahyre")
    # the first search finds the two jobs; then REDUNDANT_SEARCHES searches bring nothing new - and it stops there
    assert len(portal.searched) == 1 + REDUNDANT_SEARCHES < 12
    assert o.stats["unique_listings"] == 2 and o.stats["discovered"] == 2 * len(portal.searched)
    assert o.stats["repeat_sightings"] == o.stats["discovered"] - 2
    assert o.stats["searches_skipped_redundant"] == 12 - len(portal.searched)


async def test_a_portal_that_finds_new_jobs_is_searched_fully(clean_db, profile):
    portal = _NewEachTime()
    o = _orch(portal, profile)
    await o.discover_portal("instahyre")
    assert len(portal.searched) == 12 and o.stats["unique_listings"] == 24 and o.stats["repeat_sightings"] == 0
    assert o.stats["searches_skipped_redundant"] == 0


async def test_sightings_reconcile_with_outcomes(clean_db, profile):
    """Every sighting ends in exactly one outcome; every new record is counted once."""
    portal = _SamePortal(listings=("x", "y", "z"))
    o = _orch(portal, profile)
    await o.discover_portal("instahyre")
    s = o.stats
    outcomes = sum(s[k] for k in ("seen_before", "already_applied", "duplicates", "skipped", "expired", "too_old",
                                  "review_pending", "relevant", "job_errors"))
    assert s["discovered"] == outcomes
    assert s["new_jobs"] == s["unique_listings"] == 3


async def test_old_card_never_opens_detail_page(clean_db, profile):
    from app.matching.posting import Window
    from app.models import Job
    from sqlalchemy import select

    portal = _SamePortal()
    orch = _orch(portal, profile)
    orch.window = Window(days=1)
    old = JobPosting("instahyre", "old", "https://example.test/old", "React Developer", posted_text="3+ weeks ago")
    await orch.handle_posting(portal, old)
    with session_scope() as db:
        job = db.scalar(select(Job).where(Job.external_id == "old"))
        assert job.status == "SKIPPED" and job.match_details["unread_card"]
    assert orch.stats["too_old"] == 1


def test_queries_are_compiled_for_each_portal():
    from app.portals.queries import frontend_queries

    configured = ["React Developer", "react developer", "React Native", "AI Engineer", "DevOps", "Next.js Engineer"]
    assert frontend_queries("linkedin", configured) == ["React Developer", "Next.js Engineer"]
    assert frontend_queries("instahyre", configured) == ["React", "Next.js"]
    assert frontend_queries("naukri", configured) == ["React Developer", "Next.js Engineer"]


def test_fullstack_searches_reach_portals_and_do_not_use_frontend_only_function():
    from urllib.parse import parse_qs, urlsplit
    from app.portals.base import SearchQuery
    from app.portals.instahyre import InstahyreAdapter
    from app.portals.queries import frontend_queries
    terms = ["Full Stack Engineer", "MERN Developer", "Full Stack AI Engineer", "Python FastAPI Full Stack Engineer"]
    unrelated = ["React Sales Manager", "Graphic Designer JavaScript", "TypeScript Accountant",
                 "React Native Developer", "Full Stack Mobile Engineer"]
    assert frontend_queries("naukri", terms + unrelated) == terms
    adapter = InstahyreAdapter(None, "https://example.test")
    queries = frontend_queries("instahyre", terms + unrelated)
    params = [parse_qs(urlsplit(adapter.search_url(SearchQuery(q, location="London"))).query) for q in queries]
    assert [q["skills"][0] for q in params] == ["React", "MERN", "Generative AI", "FastAPI"]
    assert all("job_functions" not in q and q["location"] == ["London"] for q in params)


def test_remote_linkedin_search_keeps_the_selected_age_filter():
    from urllib.parse import parse_qs, urlsplit
    from app.portals.base import SearchQuery
    from app.portals.linkedin import LinkedInAdapter
    adapter = LinkedInAdapter(None, "https://example.test")
    params = parse_qs(urlsplit(adapter.search_url(SearchQuery("React", "Remote", posted_within_days=14), 1)).query)
    assert params["f_WT"] == ["2"] and "location" not in params and params["f_TPR"] == ["r1209600"]
