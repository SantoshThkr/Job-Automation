"""Skipped jobs are looked at again when the preferences that skipped them change.

A skip is recorded once and the listing is then treated as seen. That is right while your preferences stay the same -
and wrong after you edit them: on 2026-10-12 fourteen strong frontend matches on Instahyre (scores 88-99, among them
three "Senior Frontend Engineer" roles) were skipped for "location is not in your preferred locations"; the cities
were added to the profile later and the jobs stayed Skipped for good.

Every decision now carries the fingerprint of the preferences it was made with (``preferences_rev``). At the start of
a run, recent jobs that were skipped for a *hard requirement* under other preferences are evaluated again with the
rules only (no page is opened, no model is loaded):

* still blocked  -> stays Skipped, with today's reason;
* now relevant   -> Queued (the usual duplicate check applies; the run's mode decides how it is applied);
* now borderline -> Needs decision.

Never touched: jobs you skipped or rejected yourself, anything that was submitted, jobs skipped for a low score
(nothing in your preferences changed their fit), and listings older than ``max_age_days``.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from app.db import session_scope
from app.engine.recorder import add_event, find_duplicate, save_match, set_status
from app.matching.analyzer import JobInput
from app.matching.matcher import JobMatcher
from app.engine.recency import adjust, current_window
from app.matching.posting import Window, listing_closed, posted_of, verdict
from app.matching.rules import decision_reason, preferences_rev, title_prescreen
from app.models import Decision, Job, JobStatus, utcnow
from app.profile import Profile

log = logging.getLogger(__name__)

_YOURS = ("rejected by you", "skipped by you")


def _yours(job: Job) -> bool:
    """You skipped / rejected it yourself: that decision is never revisited."""
    return (job.status_reason or "").strip().lower().startswith(_YOURS)


def reconsider_skipped(profile: Profile, *, run_id: int | None = None, max_age_days: int = 30,
                       dry: bool = False, changes: list[dict] | None = None,
                       window: Window | None = None) -> dict[str, int]:
    """Re-evaluate recent jobs skipped under other preferences. Returns counts by what happened; ``changes`` (when
    given) receives one row per job whose status would change."""
    rev = preferences_rev(profile)
    matcher = JobMatcher(profile, None)
    since = utcnow() - timedelta(days=max_age_days)
    out: dict[str, int] = {}

    def count(key: str) -> None:
        out[key] = out.get(key, 0) + 1

    with session_scope() as s:
        jobs = s.scalars(select(Job).where(Job.status == JobStatus.SKIPPED, Job.discovered_at >= since)).all()
        for job in jobs:
            details: dict[str, Any] = dict(job.match_details or {})
            if details.get("profile_rev") == rev or _yours(job):
                continue
            app = job.application
            if app is not None and (app.submit_attempted or app.confirmed):
                continue
            was_blocked = bool(details.get("blockers"))
            early = title_prescreen(profile, job.title)
            if early or not was_blocked:
                # the title alone still rules it out, or it was skipped for its score: only note the preferences seen
                if not dry:
                    job.match_details = {**details, "profile_rev": rev}
                count("unchanged")
                continue
            if not (job.description or "").strip():
                # skipped from its title before the page was read: it is read and evaluated when a search shows it again
                if changes is not None:
                    from app.cli import change_row

                    changes.append(change_row(job, str(JobStatus.NEW), "Your preferences changed since it was skipped "
                                              "from its title: it is read again when a search shows it", None, profile))
                if not dry:
                    job.match_details = {**details, "profile_rev": rev}
                    set_status(s, job, JobStatus.NEW, "Your preferences changed since this was skipped from its title: "
                                                      "it is read again when a search shows it")
                    add_event(s, job.id, "reconsidered", job.status_reason, run_id=run_id)
                count("to_read_again")
                continue
            result = matcher.evaluate_rules_only(JobInput(
                title=job.title, description=job.description or "", company=job.company or "",
                location=job.location or "", experience_text=job.experience_text or "",
                salary_text=job.salary_text or "", skills=list(job.skills or [])))
            decision, age_why, _state = adjust(Decision(result.decision), "", job, window or current_window())
            if age_why and decision != Decision(result.decision):
                result.decision = decision.value
                result.flags = [*result.flags, age_why] if decision == Decision.MANUAL_REVIEW else result.flags
                result.blockers = [*result.blockers, age_why] if decision == Decision.SKIP else result.blockers
            before = (job.status_reason or "").replace("\n", " ")[:160]
            if changes is not None and decision != Decision.SKIP:
                from app.cli import change_row

                proposed = JobStatus.QUEUED if decision == Decision.APPLY else JobStatus.REVIEW_PENDING
                changes.append(change_row(job, str(proposed), f"{decision_reason(result, profile.matching.apply_threshold) or decision.value}"
                                          f" (it had been skipped: {before})", result, profile))
            if dry:
                count({Decision.SKIP: "unchanged", Decision.APPLY: "queued", Decision.MANUAL_REVIEW: "needs_decision"}[decision])
                continue
            save_match(s, job, result.to_dict())
            if decision == Decision.SKIP:
                set_status(s, job, JobStatus.SKIPPED, decision_reason(result, profile.matching.apply_threshold))
                count("unchanged")
                continue
            dup = find_duplicate(s, job)
            if dup is not None:
                set_status(s, job, JobStatus.DUPLICATE, f"Same role already handled via {dup.portal} (job #{dup.id})")
                count("duplicate")
                continue
            if decision == Decision.APPLY:
                set_status(s, job, JobStatus.QUEUED, f"Relevant (score {result.score}) - reconsidered after your "
                                                     f"preferences changed (it had been skipped: {before})")
                count("queued")
            else:
                why = decision_reason(result, profile.matching.apply_threshold) or "Needs your review"
                set_status(s, job, JobStatus.REVIEW_PENDING, f"{why} - reconsidered after your preferences changed "
                                                             f"(it had been skipped: {before})")
                count("needs_decision")
            add_event(s, job.id, "reconsidered", f"Skipped earlier ({before}); with your current preferences: "
                                                 f"{decision.value} (score {result.score})", run_id=run_id,
                      data={"score": result.score, "decision": decision.value})
    return out


def release_recency(profile: Profile, window: Window, *, run_id: int | None = None, dry: bool = False,
                    changes: list[dict] | None = None, max_age_days: int = 120) -> dict[str, int]:
    """Jobs skipped only because of their age (under a narrower window) are evaluated again once the window
    covers them - with the rules, nothing is opened. Still too old -> stays skipped; covered now -> queued, or
    yours to decide; date unknown -> yours to decide."""
    matcher = JobMatcher(profile, None)
    since = utcnow() - timedelta(days=max_age_days)
    out: dict[str, int] = {}

    def count(key: str) -> None:
        out[key] = out.get(key, 0) + 1

    with session_scope() as s:
        jobs = s.scalars(select(Job).where(Job.status == JobStatus.SKIPPED, Job.discovered_at >= since)).all()
        for job in jobs:
            details: dict[str, Any] = dict(job.match_details or {})
            if not details.get("recency_skip") or _yours(job) or listing_closed(job):
                continue
            state, said = verdict(posted_of(job), window)
            if state == "out":
                count("unchanged")
                continue
            if not (job.description or "").strip():
                proposed, why = JobStatus.NEW, "It is read again when a search shows it (your recency window changed)"
            else:
                result = matcher.evaluate_rules_only(JobInput(
                    title=job.title, description=job.description or "", company=job.company or "",
                    location=job.location or "", experience_text=job.experience_text or "",
                    salary_text=job.salary_text or "", skills=list(job.skills or [])))
                decision, why, _ = adjust(Decision(result.decision),
                                          decision_reason(result, profile.matching.apply_threshold), job, window)
                proposed = {Decision.APPLY: JobStatus.QUEUED, Decision.MANUAL_REVIEW: JobStatus.REVIEW_PENDING,
                            Decision.SKIP: JobStatus.SKIPPED}[decision]
                if proposed == JobStatus.SKIPPED:
                    count("unchanged")
                    continue
            if changes is not None:
                from app.cli import change_row

                changes.append(change_row(job, str(proposed), f"{why} (window now: {window.label()})", None, profile))
            count({JobStatus.QUEUED: "queued", JobStatus.REVIEW_PENDING: "needs_decision"}.get(proposed, "to_read_again"))
            if dry:
                continue
            details.pop("recency_skip", None)
            job.match_details = details
            set_status(s, job, proposed, f"{why} - your job-recency window is now {window.label()}"[:1000])
            add_event(s, job.id, "reconsidered", job.status_reason, run_id=run_id)
    return out
