"""Job / application endpoints."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.ai.job_assistant import JobAssistant
from app.api.serializers import job_detail, job_summary
from app.db import get_db
from app.engine.recorder import add_event, save_match, set_status
from app.matching.analyzer import JobInput
from app.matching.matcher import JobMatcher
from app.models import Application, Decision, Job, JobStatus, utcnow
from app.profile import get_profile
from app.security import require_ai_budget

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

JobId = Annotated[int, Path(ge=1, le=2**31 - 1)]
Short = Annotated[str, StringConstraints(max_length=300)]
_WORDS = r"^[A-Za-z_,\s]*$"  # status / decision / portal filters are lists of plain names


def _client(request: Request) -> str:
    return request.client.host if request.client else "local"

SORTS = {
    "date": Job.discovered_at.desc(),
    "updated": Job.updated_at.desc(),
    "score": Job.match_score.desc(),
    "company": Job.company.asc(),
}


@router.get("")
def list_jobs(
    db: Session = Depends(get_db),
    status: str | None = Query(None, description="comma separated statuses", max_length=400, pattern=_WORDS),
    portal: str | None = Query(None, max_length=32, pattern=_WORDS),
    decision: str | None = Query(None, max_length=32, pattern=_WORDS),
    q: str | None = Query(None, max_length=200),
    applications_only: bool = False,
    sort: Literal["date", "updated", "score", "company"] = "date",
    page: int = Query(1, ge=1, le=100_000),
    page_size: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    stmt = select(Job).options(selectinload(Job.application))
    count_stmt = select(func.count(Job.id))
    filters = []
    if status:
        filters.append(Job.status.in_([s.strip().upper() for s in status.split(",") if s.strip()]))
    if portal:
        filters.append(Job.portal == portal.lower())
    if decision:
        filters.append(Job.decision == decision.upper())
    if q and q.strip():
        # a bound parameter (never spliced into SQL); % and _ in your text are matched literally
        term = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{term}%"
        filters.append(or_(Job.title.ilike(like, escape="\\"), Job.company.ilike(like, escape="\\"),
                           Job.location.ilike(like, escape="\\")))
    if applications_only:
        stmt = stmt.join(Application, Application.job_id == Job.id)
        count_stmt = count_stmt.join(Application, Application.job_id == Job.id)
    for f in filters:
        stmt = stmt.where(f)
        count_stmt = count_stmt.where(f)
    total = db.scalar(count_stmt) or 0
    order = SORTS.get(sort, SORTS["date"])
    rows = db.scalars(stmt.order_by(order, Job.id.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    return {"items": [job_summary(j) for j in rows], "total": total, "page": page, "page_size": page_size}


@router.get("/{job_id}")
def get_job(job_id: JobId, db: Session = Depends(get_db)) -> dict[str, Any]:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job_detail(job)


class JobAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Annotated[str, StringConstraints(max_length=32)]  # approve|skip|retry|mark_applied|verify|park|reset
    force: bool = False
    note: Annotated[str, StringConstraints(max_length=1000)] = ""


@router.post("/{job_id}/action")
def job_action(job_id: JobId, body: JobAction, db: Session = Depends(get_db)) -> dict[str, Any]:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    action = body.action.lower()
    app = job.application
    if action == "approve":
        if job.status in {JobStatus.APPLIED, JobStatus.ALREADY_APPLIED}:
            raise HTTPException(409, "Already applied")
        set_status(db, job, JobStatus.QUEUED, "Approved by you - will apply on the next run")
        add_event(db, job.id, "user_approved", "You approved this job for application")
    elif action == "skip":
        set_status(db, job, JobStatus.SKIPPED, body.note or "Skipped by you")
        add_event(db, job.id, "user_skipped", "You skipped this job")
    elif action == "retry":
        if job.status in {JobStatus.APPLIED, JobStatus.ALREADY_APPLIED}:
            raise HTTPException(409, "Already applied")
        if app is not None and app.submit_attempted and not app.confirmed and not body.force:
            raise HTTPException(409, "A submit was already attempted for this job - check the portal first, "
                                     "then retry with force=true")
        set_status(db, job, JobStatus.QUEUED, "Queued for retry")
        add_event(db, job.id, "user_retry", "You queued this job for another attempt")
    elif action == "mark_applied":
        if app is None:
            app = Application(job_id=job.id, mode="manual", apply_target="manual")
            db.add(app)
            job.application = app
        app.status = JobStatus.APPLIED
        app.confirmed = True
        app.confirmation_text = body.note or "Marked as applied by you"
        app.applied_at = app.applied_at or utcnow()
        app.finished_at = utcnow()
        set_status(db, job, JobStatus.APPLIED, "Marked as applied by you")
        add_event(db, job.id, "user_marked_applied", "You marked this job as applied")
    elif action == "verify":
        if app is None or not app.submit_attempted:
            raise HTTPException(409, "Nothing was submitted for this job - there is nothing to verify")
        if job.status in {JobStatus.APPLIED, JobStatus.ALREADY_APPLIED}:
            raise HTTPException(409, "Already confirmed as applied")
        app.verify_requested = True
        set_status(db, job, JobStatus.QUEUED, "Re-checking whether the application went through (nothing is "
                                              "submitted again)")
        add_event(db, job.id, "user_verify", "You asked the assistant to verify the earlier submission")
    elif action == "park":
        if job.status in {JobStatus.APPLIED, JobStatus.ALREADY_APPLIED}:
            raise HTTPException(409, "Already applied")
        set_status(db, job, JobStatus.PARKED, body.note or "Parked by you - not retried until you choose Retry")
        add_event(db, job.id, "user_parked", "You parked this job")
    elif action == "reset":
        set_status(db, job, JobStatus.NEW, "Reset by you")
        add_event(db, job.id, "user_reset", "You reset this job")
    else:
        raise HTTPException(400, "Unknown action")
    db.flush()
    return job_detail(job)


class EvaluateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Annotated[str, StringConstraints(min_length=1, max_length=300)]
    description: Annotated[str, StringConstraints(max_length=60_000)] = ""
    company: Short = ""
    location: Short = ""
    experience_text: Short = ""
    salary_text: Short = ""
    skills: Annotated[list[Annotated[str, StringConstraints(max_length=80)]], Field(max_length=100)] = []
    use_ai: bool = False


@router.post("/evaluate")
async def evaluate(body: EvaluateRequest, request: Request) -> dict[str, Any]:
    if body.use_ai:
        require_ai_budget(_client(request))
    matcher = JobMatcher(get_profile(), JobAssistant() if body.use_ai else None)
    job = JobInput(title=body.title, description=body.description, company=body.company, location=body.location,
                   experience_text=body.experience_text, salary_text=body.salary_text, skills=body.skills)
    result = await matcher.evaluate(job, use_ai=body.use_ai)
    return result.to_dict()


@router.post("/{job_id}/reevaluate")
async def reevaluate(job_id: JobId, request: Request, use_ai: bool = False,
                     db: Session = Depends(get_db)) -> dict[str, Any]:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if use_ai:
        require_ai_budget(_client(request))
    matcher = JobMatcher(get_profile(), JobAssistant() if use_ai else None)
    result = await matcher.evaluate(JobInput(title=job.title, description=job.description, company=job.company,
                                             location=job.location, experience_text=job.experience_text,
                                             salary_text=job.salary_text, skills=job.skills or []), use_ai=use_ai)
    save_match(db, job, result.to_dict())
    if job.status in {JobStatus.NEW, JobStatus.SKIPPED, JobStatus.REVIEW_PENDING, JobStatus.QUEUED}:
        new_status = {Decision.APPLY: JobStatus.QUEUED, Decision.SKIP: JobStatus.SKIPPED,
                      Decision.MANUAL_REVIEW: JobStatus.REVIEW_PENDING}[Decision(result.decision)]
        set_status(db, job, new_status, "; ".join(result.blockers or result.flags or result.reasons[:1]))
    add_event(db, job.id, "reevaluated", f"Re-evaluated: {result.decision} ({result.score})")
    db.flush()
    return job_detail(job)
