"""What a run did, counted so that the numbers reconcile: sightings vs unique listings vs new records, and for every job
the run decided about, one outcome - with the rule that produced it (a reason code), the stage (title alone, the read
description, the posting date, duplicates, the queue) and who decided (the rules, or the rules with the local model).

Background (2026-10-15): the dashboard showed run 76 as "1 832 discovered, 106 new, 77 skipped, 19 for a decision,
7 relevant, 2 duplicates" - sightings next to job counts, with the 238 old queued jobs the run set aside not shown at all.
Built only from what the run stored (its events and the job records); nothing is changed."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.matching.reasons import MEANING, code_of
from app.models import ApplicationEvent, Job, Run

# discovery outcomes, in the order a listing can end
OUTCOMES = ("relevant", "review", "skipped", "skipped_title", "too_old", "closed", "duplicate", "error")
_QUEUE_RESULTS = {"result_applied": "applied", "result_dry_run": "dry_run", "result_manual_action_required": "needs_you",
                  "result_failed": "failed", "result_expired": "closed", "result_already_applied": "already_applied"}


def _discovery_outcome(ev: ApplicationEvent) -> tuple[str, str, str] | None:
    """(outcome, reason text, stage) for an event that ends a listing's discovery; None for any other event."""
    msg = ev.message or ""
    if ev.type == "evaluated":
        m = re.match(r"(\w+) \((title|score [\d.]+)\): ?(.*)", msg, re.S)
        decision, kind, why = (m.group(1), m.group(2), m.group(3)) if m else (msg.split(" ")[0], "", msg)
        if decision == "SKIP":
            return ("skipped_title" if kind == "title" else "skipped"), why, ("title" if kind == "title" else "read")
        return ("review" if decision == "MANUAL_REVIEW" else "relevant"), why, "read"
    if ev.type == "recency" and not msg.startswith("Not applied"):
        return "too_old", msg, "posting date"
    if ev.type == "expired":
        return "closed", msg, "read"
    if ev.type == "duplicate":
        return "duplicate", msg, "duplicates"
    if ev.type == "error":
        return "error", msg, "read"
    return None


def run_breakdown(db: Session, run_id: int, *, audit: bool = False, profile: Any = None) -> dict[str, Any] | None:
    run = db.get(Run, run_id)
    if run is None:
        return None
    stats = dict(run.stats or {})
    events = db.scalars(select(ApplicationEvent).where(ApplicationEvent.run_id == run_id,
                                                       ApplicationEvent.job_id.is_not(None))
                        .order_by(ApplicationEvent.id)).all()
    jobs = {j.id: j for j in db.scalars(select(Job).where(Job.id.in_({e.job_id for e in events}))).all()} if events else {}

    decided: dict[int, tuple[str, str, str]] = {}  # job -> its discovery outcome (the first one: one per job)
    queue: dict[int, str] = {}
    for ev in events:
        found = _discovery_outcome(ev)
        if found and ev.job_id not in decided:
            decided[ev.job_id] = found
        if ev.type == "recency" and (ev.message or "").startswith("Not applied"):
            queue[ev.job_id] = "set_aside_by_date" if "automatically" not in ev.message else "date_unknown"
        elif ev.type in _QUEUE_RESULTS:
            queue[ev.job_id] = _QUEUE_RESULTS[ev.type]

    by_outcome: Counter[str] = Counter()
    by_portal: dict[str, Counter[str]] = defaultdict(Counter)
    by_stage: Counter[str] = Counter()
    by_decider: Counter[str] = Counter()
    reasons: dict[str, dict[str, Any]] = {}
    for job_id, (outcome, why, stage) in decided.items():
        job = jobs.get(job_id)
        portal = job.portal if job else "?"
        by_outcome[outcome] += 1
        by_portal[portal][outcome] += 1
        by_stage[stage] += 1
        details = (job.match_details or {}) if job else {}
        if outcome in {"relevant", "review", "skipped"}:
            by_decider[details.get("decided_by") or ("rules+ai" if (details.get("ai") or {}).get("used") else "rules")] += 1
        if outcome == "relevant":
            continue
        code = {"too_old": "TOO_OLD", "closed": "CLOSED", "duplicate": "DUPLICATE", "error": "ERROR"}.get(outcome) \
            or (code_of(why.split(";")[0]) if why else "LOW_SCORE")
        if code == "OTHER" and re.match(r"Low match", why or ""):
            code = "LOW_SCORE"
        entry = reasons.setdefault(f"{outcome}:{code}", {"outcome": outcome, "code": code,
                                                          "meaning": MEANING.get(code, ""), "count": 0, "examples": []})
        entry["count"] += 1
        if len(entry["examples"]) < 5 and job is not None:
            entry["examples"].append({"id": job.id, "title": job.title, "company": job.company, "portal": job.portal,
                                      "why": (why or "")[:300]})

    discovered = int(stats.get("discovered", 0))
    accounted = sum(int(stats.get(k, 0)) for k in ("seen_before", "already_applied", "duplicates", "skipped",
                                                   "expired", "review_pending", "relevant", "job_errors"))
    discovery_too_old = by_outcome["too_old"]
    out: dict[str, Any] = {
        "run": {"id": run.id, "mode": run.mode, "status": run.status, "portals": run.portals or []},
        "funnel": {
            "sightings": discovered,  # every listing a search showed, a job listed by two searches twice
            "unique_listings": stats.get("unique_listings"),  # (recorded from 2026-10-15 on)
            "repeat_sightings": stats.get("repeat_sightings"),
            "known_sightings": int(stats.get("seen_before", 0)),
            "new_records": int(stats.get("new_jobs", 0)),
            "read_again": int(stats.get("read_again", 0)),
            "already_applied_on_portal": int(stats.get("already_applied", 0)),
            "searches_skipped_redundant": int(stats.get("searches_skipped_redundant", 0)),
        },
        "discovery": {
            "jobs": len(decided),
            "by_outcome": {k: by_outcome[k] for k in OUTCOMES if by_outcome[k]},
            "by_portal": {p: {k: c[k] for k in OUTCOMES if c[k]} for p, c in sorted(by_portal.items())},
            "by_stage": dict(by_stage),
            "decided_by": dict(by_decider),
            "reasons": sorted(reasons.values(), key=lambda r: -r["count"]),
        },
        "queue": dict(Counter(queue.values())),
        "reconciles": discovered == accounted + discovery_too_old,
        "reconciliation": (f"{discovered} sightings = {stats.get('seen_before', 0)} already known + "
                           f"{len(decided)} decided in this run ("
                           + ", ".join(f"{by_outcome[k]} {k.replace('_', ' ')}" for k in OUTCOMES if by_outcome[k])
                           + f"){' + ' + str(stats.get('already_applied')) + ' already applied' if stats.get('already_applied') else ''}"),
    }
    if audit and profile is not None:
        out["audit"] = audit_skips(profile, [jobs[j] for j, (o, _w, _s) in decided.items()
                                             if o in {"skipped", "skipped_title"} and j in jobs])
    return out


def audit_skips(profile: Any, jobs: list[Job]) -> dict[str, Any]:
    """The run's skips judged again by today's rules (read-only): the ones they would now apply to, put to you, or
    read before deciding (skipped from the title alone) are possible false skips - to be checked, not counted as
    errors."""
    from app.matching.analyzer import JobInput
    from app.matching.matcher import JobMatcher
    from app.matching.rules import title_prescreen

    matcher = JobMatcher(profile, None)
    now: Counter[str] = Counter()
    examples: list[dict[str, Any]] = []
    for job in jobs:
        if not (job.description or "").strip():
            verdict = "skip" if title_prescreen(profile, job.title or "") else "read_now"
        else:
            r = matcher.evaluate_rules_only(JobInput(title=job.title or "", description=job.description or "",
                                                     company=job.company or "", location=job.location or "",
                                                     experience_text=job.experience_text or "",
                                                     salary_text=job.salary_text or "", skills=list(job.skills or [])))
            verdict = {"APPLY": "apply_now", "MANUAL_REVIEW": "review_now"}.get(r.decision, "skip")
        now[verdict] += 1
        if verdict != "skip" and len(examples) < 15:
            examples.append({"id": job.id, "title": job.title, "company": job.company, "now": verdict})
    return {"skips_checked": len(jobs), "still_skipped": now["skip"], "would_apply": now["apply_now"],
            "would_put_to_you": now["review_now"], "would_read_first": now["read_now"], "examples": examples}
