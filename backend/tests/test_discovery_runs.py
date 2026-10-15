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
