"""An application is sent once: the same role is recognised across portals and listings, and a job that was applied
to - or may have been - is never put back in the queue without you saying so."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.db import session_scope
from app.engine.recorder import find_duplicate, set_status, start_application, upsert_job
from app.models import AgentMode, Job, JobStatus, utcnow
from app.portals.types import JobPosting


def _job(s, portal: str, ext: str, title: str = "Senior Frontend Engineer", company: str = "Kitco Labs") -> Job:
    job, _ = upsert_job(s, JobPosting(portal=portal, external_id=ext, url=f"https://{portal}.test/{ext}", title=title,
                                      company=company, location="Bengaluru", description="React, TypeScript"))
    return job


def test_the_same_role_on_another_portal_or_listing_is_a_duplicate(clean_db):
    with session_scope() as s:
        applied = _job(s, "naukri", "d-1")
        set_status(s, applied, JobStatus.APPLIED, "Applied on Naukri")
        twin = _job(s, "linkedin", "d-2")  # same employer and title, found on LinkedIn
        relisted = _job(s, "naukri", "d-3", title="senior  frontend engineer", company="KITCO LABS")  # the same, re-listed
        other_role = _job(s, "linkedin", "d-4", title="Frontend Engineer")
        other_company = _job(s, "linkedin", "d-5", company="Other Labs")
        assert find_duplicate(s, twin).id == applied.id and find_duplicate(s, relisted).id == applied.id
        assert find_duplicate(s, other_role) is None and find_duplicate(s, other_company) is None
        assert find_duplicate(s, applied) is None  # a job is not its own duplicate


@pytest.mark.parametrize("status,blocks", [
    (JobStatus.APPLIED, True), (JobStatus.ALREADY_APPLIED, True), (JobStatus.APPLYING, True),
    (JobStatus.AWAITING_APPROVAL, True),
    # nothing was sent for these: the role may still be applied to through the other listing
    (JobStatus.SKIPPED, False), (JobStatus.FAILED, False), (JobStatus.QUEUED, False), (JobStatus.EXPIRED, False),
])
def test_only_work_that_was_or_is_being_sent_blocks_the_other_listing(clean_db, status, blocks):
    with session_scope() as s:
        first = _job(s, "naukri", "d-1")
        set_status(s, first, status, "x")
        twin = _job(s, "linkedin", "d-2")
        assert (find_duplicate(s, twin) is not None) is blocks


def test_an_application_from_long_ago_does_not_block_a_new_opening(clean_db):
    with session_scope() as s:
        old = _job(s, "naukri", "d-1")
        set_status(s, old, JobStatus.APPLIED, "Applied on Naukri")
        old.updated_at = old.discovered_at = utcnow() - timedelta(days=200)
        s.flush()
        assert find_duplicate(s, _job(s, "linkedin", "d-2")) is None


@pytest.fixture
def api(clean_db):
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


def test_applied_and_possibly_sent_jobs_are_not_requeued_by_a_click(api):
    with session_scope() as s:
        done = _job(s, "naukri", "d-1")
        set_status(s, done, JobStatus.APPLIED, "Applied on Naukri")
        unsure = _job(s, "linkedin", "d-2", company="Unsure Labs")
        app = start_application(s, unsure, AgentMode.AUTO, resume_id=None, resume_path=None)
        app.submit_attempted = True
        set_status(s, unsure, JobStatus.MANUAL_ACTION_REQUIRED, "Submitted - confirmation not verified")
        done_id, unsure_id = done.id, unsure.id
    for action in ("retry", "approve"):
        assert api.post(f"/api/jobs/{done_id}/action", json={"action": action}).status_code == 409
    refused = api.post(f"/api/jobs/{unsure_id}/action", json={"action": "retry"})
    assert refused.status_code == 409 and "already attempted" in refused.json()["detail"]
    with session_scope() as s:
        assert s.get(Job, done_id).status == JobStatus.APPLIED
        assert s.get(Job, unsure_id).status == JobStatus.MANUAL_ACTION_REQUIRED
    # only your explicit confirmation (after checking the site) lets it be tried again
    assert api.post(f"/api/jobs/{unsure_id}/action", json={"action": "retry", "force": True}).status_code == 200
