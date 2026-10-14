"""The job-recency window as the whole workflow uses it: search, discovery, the queue, re-evaluation and the last
check before an application is sent. The window itself is chosen in the dashboard and kept in the agent state."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from app.db import session_scope
from app.matching.posting import Posted, Window, listing_closed, posted_of, verdict, window_from
from app.models import AgentState, Decision


def current_window() -> Window:
    """The window chosen in the dashboard (the last 7 days until one is chosen)."""
    with session_scope() as s:
        return window_from(AgentState.get(s).recency)


def adjust(decision: Decision, why: str, job: Any, window: Window) -> tuple[Decision, str, str]:
    """A matching decision with the job's age taken into account: (decision, reason, recency state).

    * posted outside the window -> SKIP, saying when it was posted;
    * posting date unknown (or "3+ weeks ago" against a longer window) -> an APPLY becomes yours to decide;
    * closed by its own end date -> SKIP.
    A score never brings an old job back."""
    closed = listing_closed(job)
    if closed:
        return Decision.SKIP, f"Closed: {closed}", "out"
    state, said = verdict(posted_of(job), window)
    if state == "out":
        return Decision.SKIP, said[0].upper() + said[1:], state
    if state == "unknown" and decision == Decision.APPLY:
        return Decision.MANUAL_REVIEW, f"{why} - but {said}" if why else said[0].upper() + said[1:], state
    return decision, why, state


_RANK = {"in": 0, "unknown": 1, "out": 2}


def strictest(posted: Posted | None, windows: list[Window]) -> tuple[str, str, Window]:
    """The verdict of the strictest of several windows (the run's and the one chosen since): (state, why, window)."""
    found: tuple[str, str, Window] | None = None
    for window in windows:
        state, said = verdict(posted, window)
        if found is None or _RANK[state] > _RANK[found[0]]:
            found = (state, said, window)
    assert found is not None, "at least one window"
    return found


async def page_dates(page: Any) -> SimpleNamespace:
    """The posting date and end date the open page states about itself (JSON-LD), if any."""
    from app.portals.common import read_page_dates

    found = SimpleNamespace(posted_at=None, posted_date_only=False, valid_through=None)
    if page is not None:
        try:
            await read_page_dates(page, found)
        except Exception:  # noqa: BLE001 - optional evidence
            pass
    return found


async def final_check(job: Any, window: Window | list[Window], page: Any, *, unknown_blocks: bool,
                      on_pass: Callable[[str], Any] | None = None,
                      on_page_dates: Callable[[SimpleNamespace], Any] | None = None) -> str:
    """'' or why this application must not be sent - right before the irreversible click. The page's own dates
    (when it is the job page) take precedence over what was recorded when the job was found. Several windows: the
    strictest decides. ``on_pass`` hears what was checked when the application may go ahead (for the timeline);
    ``on_page_dates`` receives the dates the page states, so the job's record can keep them."""
    fresh = await page_dates(page)
    if on_page_dates is not None and (fresh.posted_at is not None or fresh.valid_through is not None):
        on_page_dates(fresh)
    if fresh.valid_through is not None:
        job = SimpleNamespace(**{**_fields(job), "valid_through": fresh.valid_through})
    if fresh.posted_at is not None:
        posted: Posted | None = Posted(fresh.posted_at, date_only=fresh.posted_date_only, source="page")
    else:
        posted = posted_of(job)
    closed = listing_closed(job)
    if closed:
        return f"the job is closed - {closed}"
    state, said, _ = strictest(posted, window if isinstance(window, list) else [window])
    if state == "out" or (state == "unknown" and unknown_blocks):
        return said
    if on_pass is not None:
        on_pass(said if state == "in" else f"{said} (going ahead only because you approve it before it is sent)")
    return ""


def _fields(job: Any) -> dict:
    keys = ("posted_at", "posted_open_ended", "posted_date_only", "posted_source", "posted_spread_s", "posted_text",
            "discovered_at", "valid_through")
    return {k: getattr(job, k, None) for k in keys}


def snapshot(job: Any) -> SimpleNamespace:
    """The posting fields of a job record, safe to use after its database session has ended."""
    return SimpleNamespace(**_fields(job))
