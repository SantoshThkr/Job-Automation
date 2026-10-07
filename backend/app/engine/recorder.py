"""Persistence helpers shared by the agent and the API (jobs, applications, events, answers)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.answers.question_bank import Answer, QuestionContext, normalize_question
from app.db import session_scope
from app.models import (
    AgentMode,
    AnswerMemory,
    Application,
    ApplicationEvent,
    Job,
    JobStatus,
    QuestionAnswer,
    utcnow,
)
from app.portals.types import JobPosting

_COMPANY_NOISE = re.compile(
    r"\b(?:pvt|private|ltd|limited|llp|inc|corp|corporation|co|company|technologies|technology|tech|"
    r"solutions|services|software|systems|labs|india|global|group)\b\.?", re.I)
_TITLE_NOISE = re.compile(r"\(.*?\)|\[.*?\]|\b(?:urgent(?:ly)?|hiring|opening|immediate\s+joiners?|wfh|remote|"
                          r"hybrid|onsite|for|-)\b", re.I)


def fingerprint(company: str, title: str) -> str:
    c = re.sub(r"[^a-z0-9 ]+", " ", _COMPANY_NOISE.sub(" ", (company or "").lower()))
    t = re.sub(r"[^a-z0-9+#. ]+", " ", _TITLE_NOISE.sub(" ", (title or "").lower()))
    c = re.sub(r"\s+", " ", c).strip()
    t = re.sub(r"\s+", " ", t).strip()
    return f"{c}|{t}" if c or t else ""


def local_day_start_utc(days_ago: int = 0) -> datetime:
    """Midnight (local machine time) N days ago, as naive UTC."""
    now_local = datetime.now().astimezone()
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_ago)
    return start_local.astimezone(UTC).replace(tzinfo=None)


# ------------------------------------------------------------------ jobs ---

def upsert_job(s: Session, p: JobPosting, *, run_id: int | None = None, query: str = "") -> tuple[Job, bool]:
    """Insert or update a job by (portal, external_id). Returns (job, created)."""
    job = s.scalar(select(Job).where(Job.portal == p.portal, Job.external_id == p.external_id))
    created = job is None
    if job is None:
        job = Job(portal=p.portal, external_id=p.external_id, status=JobStatus.NEW, run_id=run_id,
                  search_query=query)
        s.add(job)
    job.url = p.url or job.url
    job.title = p.title or job.title
    job.company = p.company or job.company
    job.location = p.location or job.location
    job.experience_text = p.experience_text or job.experience_text
    job.salary_text = p.salary_text or job.salary_text
    job.posted_text = p.posted_text or job.posted_text
    if p.description and len(p.description) >= len(job.description or ""):
        job.description = p.description
    elif not job.description and p.snippet:
        job.description = p.snippet
    if p.skills:
        job.skills = list(dict.fromkeys(p.skills))
    if p.apply_type != "unknown":
        job.apply_type = p.apply_type
    if p.external_url:
        job.external_url = p.external_url
    job.fingerprint = fingerprint(job.company, job.title)
    s.flush()
    return job, created


def find_duplicate(s: Session, job: Job, days: int = 60) -> Job | None:
    """Same company+title already applied/applying via another portal (or another listing id)."""
    if not job.fingerprint or job.fingerprint.startswith("|"):
        return None
    since = utcnow() - timedelta(days=days)
    return s.scalar(
        select(Job).where(
            Job.fingerprint == job.fingerprint,
            Job.id != job.id,
            Job.status.in_([JobStatus.APPLIED, JobStatus.ALREADY_APPLIED, JobStatus.APPLYING,
                            JobStatus.AWAITING_APPROVAL]),
            Job.updated_at >= since,
        ).limit(1)
    )


def save_match(s: Session, job: Job, result: dict[str, Any]) -> None:
    job.match_score = result.get("score")
    job.decision = result.get("decision")
    job.match_details = result
    job.resume_id = result.get("resume_id")
    job.evaluated_at = utcnow()


def set_status(s: Session, job: Job, status: JobStatus | str, reason: str = "") -> None:
    job.status = str(status)
    if reason:
        job.status_reason = reason
    if job.application is not None and status in {
        JobStatus.APPLYING, JobStatus.AWAITING_APPROVAL, JobStatus.APPLIED, JobStatus.DRY_RUN,
        JobStatus.MANUAL_ACTION_REQUIRED, JobStatus.FAILED, JobStatus.ALREADY_APPLIED, JobStatus.SKIPPED,
    }:
        job.application.status = str(status)


def add_event(s: Session, job_id: int | None, type_: str, message: str, *, level: str = "info",
              application_id: int | None = None, run_id: int | None = None, data: dict | None = None,
              screenshot: str | None = None) -> ApplicationEvent:
    ev = ApplicationEvent(job_id=job_id, application_id=application_id, run_id=run_id, type=type_,
                          message=message[:4000], level=level, data=_jsonable(data or {}), screenshot=screenshot)
    s.add(ev)
    s.flush()
    return ev


def _jsonable(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, (list, tuple)):
            out[k] = [x if isinstance(x, (str, int, float, bool)) or x is None else str(x) for x in v][:50]
        elif isinstance(v, dict):
            out[k] = _jsonable(v)
        else:
            out[k] = str(v)
    return out


# ---------------------------------------------------------- applications ---

def start_application(s: Session, job: Job, mode: AgentMode | str, *, resume_id: str | None,
                      resume_path: str | None, target: str = "portal") -> Application:
    app = job.application
    if app is None:
        app = Application(job_id=job.id)
        s.add(app)
        job.application = app
    app.mode = str(mode)
    app.apply_target = target
    app.resume_id = resume_id
    app.resume_path = resume_path
    app.attempts = (app.attempts or 0) + 1
    app.status = JobStatus.APPLYING
    app.started_at = utcnow()
    app.finished_at = None
    app.failure_reason = ""
    app.manual_action_reason = ""
    app.review_decision = None
    app.last_step = "started"
    if app.id is not None:
        for qa in list(app.answers):
            s.delete(qa)
    job.status = JobStatus.APPLYING
    s.flush()
    return app


def record_answer(s: Session, application_id: int, q: QuestionContext, a: Answer) -> QuestionAnswer:
    nq = normalize_question(q.question)
    existing = s.scalar(select(QuestionAnswer).where(QuestionAnswer.application_id == application_id,
                                                     QuestionAnswer.normalized_question == nq))
    row = existing or QuestionAnswer(application_id=application_id, question=q.question[:2000],
                                     normalized_question=nq)
    row.field_type = q.field_type
    row.options = [o for o in q.options][:60]
    row.required = q.required
    row.answer = a.value
    row.confidence = round(a.confidence, 3)
    row.source = a.source
    row.needs_review = a.needs_review
    row.note = (a.note or "")[:1000]
    if existing is None:
        s.add(row)
    s.flush()
    return row


def lookup_memory(s: Session, normalized_question: str) -> str | None:
    row = s.scalar(select(AnswerMemory).where(AnswerMemory.normalized_question == normalized_question))
    if row is None:
        return None
    row.times_used = (row.times_used or 0) + 1
    return row.answer


def remember_answer(s: Session, question: str, answer: str) -> AnswerMemory:
    nq = normalize_question(question)
    row = s.scalar(select(AnswerMemory).where(AnswerMemory.normalized_question == nq))
    if row is None:
        row = AnswerMemory(normalized_question=nq, question=question, answer=answer)
        s.add(row)
    else:
        row.answer = answer
        row.question = question
    s.flush()
    return row


def memory_lookup_fn():
    """A lookup callable for the Answerer that opens its own short session."""

    def lookup(nq: str) -> str | None:
        with session_scope() as s:
            return lookup_memory(s, nq)

    return lookup


def applications_since(s: Session, since: datetime) -> int:
    return s.scalar(select(func.count(Application.id)).where(
        Application.applied_at.is_not(None), Application.applied_at >= since)) or 0
