"""Data types shared by portal and ATS adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from app.matching.analyzer import JobInput


@dataclass
class JobPosting:
    portal: str
    external_id: str
    url: str
    title: str
    company: str = ""
    location: str = ""
    experience_text: str = ""
    salary_text: str = ""
    posted_text: str = ""
    snippet: str = ""
    description: str = ""
    skills: list[str] = field(default_factory=list)
    already_applied: bool = False
    apply_type: str = "unknown"  # portal | external | unknown
    external_url: str | None = None
    easy_apply: bool | None = None
    details_loaded: bool = False
    # the portal's own posting date on the job page (naive UTC) and the listing's end date, when the page has them
    posted_at: datetime | None = None
    posted_date_only: bool = False
    valid_through: datetime | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_job_input(self) -> JobInput:
        return JobInput(title=self.title, description=self.description or self.snippet, company=self.company,
                        location=self.location, experience_text=self.experience_text,
                        salary_text=self.salary_text, skills=list(self.skills))


class ApplyOutcome(StrEnum):
    APPLIED = "APPLIED"
    DRY_RUN = "DRY_RUN"
    MANUAL_ACTION_REQUIRED = "MANUAL_ACTION_REQUIRED"
    FAILED = "FAILED"
    ALREADY_APPLIED = "ALREADY_APPLIED"
    EXTERNAL = "EXTERNAL"  # hand over to the ATS layer
    REJECTED = "REJECTED"  # user rejected in review mode
    EXPIRED = "EXPIRED"  # the job is closed / no longer accepting applications


@dataclass
class ApplyResult:
    outcome: ApplyOutcome
    message: str = ""
    confirmation_text: str = ""
    external_url: str | None = None
    external_page: Any = None  # playwright Page of an external tab
    ats: str | None = None
    submit_attempted: bool = False
    screenshot: str | None = None
    unresolved: list[str] = field(default_factory=list)
    resume_url: str | None = None  # page to continue from on a retry (parked external applications)
    steps: int = 0
    handoff: bool = False  # a specialised adapter gave up structurally: the generic navigator may continue
    verification: str = ""  # confirmed | uncertain | failed (engine/submission.py) - "" when nothing was submitted
    evidence: list[str] = field(default_factory=list)  # the signals the verdict rests on
    # the portal itself refuses further applications for now (daily quota, rate limit): what it said. The job is not
    # at fault and nothing about it needs you - it stays queued and the portal is left alone for the rest of the run
    portal_limit: str = ""
    # verified afterwards that nothing reached the portal (e.g. the reloaded job still offers "Apply"): the earlier
    # "submit attempted" mark is withdrawn, so Retry needs no confirmation and nothing has to be checked by you
    nothing_sent: bool = False
    click_ignored: bool = False  # the portal did not react to its own Apply click at all (see orchestrator)
    # the portal's session ended in the middle of this application (what it showed). Not this job's problem: unless
    # something may already have been sent, the job goes back to the queue and the portal waits for your next login
    login_required: str = ""
    # the attempt broke for a reason that has nothing to do with the job (the internet connection dropped, the
    # browser window was closed): what happened. Unless something may already have been sent, the job goes back to
    # the queue for a later run - a few times, then it is recorded as failed
    transient: str = ""
