"""A run's breakdown: sightings vs jobs, one outcome per job, the rule behind it - and numbers that reconcile."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.db import session_scope
from app.engine.recorder import add_event, set_status, upsert_job
from app.models import JobStatus, Run
from app.portals.types import JobPosting


def _job(s, ext, title, status, reason, portal="linkedin", details=None):
    job, _ = upsert_job(s, JobPosting(portal=portal, external_id=ext, url=f"https://{portal}.test/{ext}", title=title,
                                      company=f"Co {ext}", description="React, TypeScript" if details else ""))
    job.match_details = details or {}
    set_status(s, job, status, reason)
    s.flush()
    return job


def _run(clean_db_unused=None) -> int:
    with session_scope() as s:
        run = Run(mode="auto", portals=["linkedin", "instahyre"], status="COMPLETED",
                  stats={"discovered": 12, "seen_before": 6, "new_jobs": 6, "skipped": 3, "review_pending": 1,
                         "relevant": 1, "duplicates": 1, "too_old": 2, "unique_listings": 9, "repeat_sightings": 3})
        s.add(run)
        s.flush()
        rid = run.id
        a = _job(s, "a", "Senior Frontend Engineer", JobStatus.QUEUED, "Relevant (score 92)", details={"decided_by": "rules"})
        b = _job(s, "b", "Data Engineer", JobStatus.SKIPPED, "Title contains excluded keyword 'data engineer'")
        c = _job(s, "c", "Generative AI Engineer", JobStatus.SKIPPED, "Title looks like a data role", portal="instahyre")
        d = _job(s, "d", "Software Engineer", JobStatus.SKIPPED, "Not a frontend role", details={"decided_by": "rules"})
        e = _job(s, "e", "Full Stack Engineer", JobStatus.REVIEW_PENDING, "Full-stack role is backend-heavy (frontend ≈ 40%)",
                 details={"decided_by": "rules (uncertain)"})
        f = _job(s, "f", "Senior Frontend Engineer", JobStatus.DUPLICATE, "Same role already handled via naukri (job #1)")
        old = _job(s, "g", "React Developer", JobStatus.SKIPPED, "Not applied: Posted at least 25 days ago ...")
        add_event(s, a.id, "evaluated", "APPLY (score 92.0): Relevant (score 92)", run_id=rid)
        add_event(s, a.id, "evaluated", "APPLY (score 92.0): Relevant (score 92)", run_id=rid)  # listed twice: one job
        add_event(s, b.id, "evaluated", "SKIP (title): Title contains excluded keyword 'data engineer'", run_id=rid)
        add_event(s, c.id, "evaluated", "SKIP (title): Title looks like a data role", run_id=rid)
        add_event(s, d.id, "evaluated", "SKIP (score 41.0): Not a frontend role: frontend focus ≈ 15% - the description "
                                        "names no frontend technology", run_id=rid)
        add_event(s, e.id, "evaluated", "MANUAL_REVIEW (score 80.0): Full-stack role is backend-heavy (frontend ≈ 40%)",
                  run_id=rid)
        add_event(s, f.id, "duplicate", "Same role already handled via naukri (job #1)", run_id=rid)
        add_event(s, old.id, "recency", "Not applied: Posted at least 25 days ago: outside your job-recency window",
                  run_id=rid)
        return rid


def test_a_run_is_counted_once_per_job_and_reconciles(clean_db):
    from app.engine.run_report import run_breakdown

    rid = _run()
    with session_scope() as s:
        out = run_breakdown(s, rid)
    assert out["funnel"]["sightings"] == 12 and out["funnel"]["unique_listings"] == 9 and out["funnel"]["new_records"] == 6
    d = out["discovery"]
    assert d["jobs"] == 6  # job a was evaluated twice in the run: counted once
    assert d["by_outcome"] == {"relevant": 1, "review": 1, "skipped": 1, "skipped_title": 2, "duplicate": 1}
    assert d["by_portal"]["instahyre"] == {"skipped_title": 1}
    assert d["by_stage"] == {"read": 3, "title": 2, "duplicates": 1}
    codes = {(r["outcome"], r["code"]) for r in d["reasons"]}
    assert ("skipped_title", "TITLE_EXCLUDED_KEYWORD") in codes and ("skipped_title", "TITLE_OTHER_FAMILY") in codes
    assert ("skipped", "FRONTEND_SHARE_LOW") in codes and ("review", "FULLSTACK_BACKEND_HEAVY") in codes
    assert out["queue"] == {"set_aside_by_date": 1}  # an old queued job: not mixed into the run's new listings
    assert out["reconciles"] is True and out["reconciliation"].startswith("12 sightings = 6 already known + 6 decided")


def test_the_audit_judges_the_runs_skips_again_without_changing_them(clean_db, profile):
    from app.engine.run_report import run_breakdown

    rid = _run()
    with session_scope() as s:
        out = run_breakdown(s, rid, audit=True, profile=profile)
    a = out["audit"]
    assert a["skips_checked"] == 3
    assert a["would_read_first"] == 1 and a["examples"][0]["title"] == "Generative AI Engineer"  # no longer skipped unread
    from sqlalchemy import select

    from app.models import Job

    with session_scope() as s:  # judged again, not changed
        assert s.scalar(select(Job).where(Job.external_id == "c")).status == JobStatus.SKIPPED


def test_the_endpoint(clean_db):
    from app.main import create_app

    rid = _run()
    with TestClient(create_app()) as c:
        r = c.get(f"/api/agent/runs/{rid}/breakdown")
        assert r.status_code == 200 and r.json()["reconciles"] is True and "audit" not in r.json()
        assert c.get(f"/api/agent/runs/{rid}/breakdown?audit=true").json()["audit"]["skips_checked"] == 3
        assert c.get("/api/agent/runs/999999/breakdown").status_code == 404
        assert c.get("/api/agent/runs/0/breakdown").status_code == 422
