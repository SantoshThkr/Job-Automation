"""Stats, events, answers memory, AI status, screenshots and health endpoints."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.ai.provider import get_provider
from app.api.serializers import event_dict, memory_dict, qa_dict
from app.config import get_settings
from app.db import get_db
from app.engine.recorder import local_day_start_utc, remember_answer
from app.models import AnswerMemory, Application, ApplicationEvent, Decision, Job, JobStatus, QuestionAnswer

router = APIRouter(prefix="/api", tags=["misc"])


@router.get("/health")
def health(db: Session = Depends(get_db)) -> dict[str, Any]:
    db.scalar(select(func.count(Job.id)))
    return {"ok": True}


@router.get("/stats")
def stats(db: Session = Depends(get_db)) -> dict[str, Any]:
    def count(*where) -> int:
        stmt = select(func.count(Job.id))
        for w in where:
            stmt = stmt.where(w)
        return db.scalar(stmt) or 0

    by_status = dict(db.execute(select(Job.status, func.count(Job.id)).group_by(Job.status)).all())
    by_portal = dict(db.execute(select(Job.portal, func.count(Job.id)).group_by(Job.portal)).all())
    today = local_day_start_utc(0)
    week = local_day_start_utc(6)
    applied_today = db.scalar(select(func.count(Application.id)).where(
        Application.status == JobStatus.APPLIED, Application.applied_at >= today)) or 0
    applied_week = db.scalar(select(func.count(Application.id)).where(
        Application.status == JobStatus.APPLIED, Application.applied_at >= week)) or 0
    manual = sum(by_status.get(s, 0) for s in (JobStatus.MANUAL_ACTION_REQUIRED, JobStatus.REVIEW_PENDING,
                                                 JobStatus.AWAITING_APPROVAL))
    return {
        "jobs_discovered": count(),
        "relevant_jobs": count(or_(Job.decision == Decision.APPLY, Job.status == JobStatus.QUEUED,
                                   Job.application.has())),
        "applications": by_status.get(JobStatus.APPLIED, 0),
        "skipped": by_status.get(JobStatus.SKIPPED, 0) + by_status.get(JobStatus.DUPLICATE, 0),
        "failed": by_status.get(JobStatus.FAILED, 0),
        "manual_action_required": manual,
        "dry_run": by_status.get(JobStatus.DRY_RUN, 0),
        "already_applied": by_status.get(JobStatus.ALREADY_APPLIED, 0),
        "queued": by_status.get(JobStatus.QUEUED, 0),
        "today_applications": applied_today,
        "weekly_applications": applied_week,
        "by_status": by_status,
        "by_portal": by_portal,
    }


@router.get("/events")
def recent_events(limit: int = 50, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = db.scalars(select(ApplicationEvent).order_by(ApplicationEvent.id.desc()).limit(min(limit, 300))).all()
    return [event_dict(e) | {"job_title": e.job.title if e.job else None,
                             "job_company": e.job.company if e.job else None} for e in rows]


@router.get("/questions/pending")
def pending_questions(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    """Questions the assistant could not answer confidently, for jobs that still need action."""
    rows = db.execute(
        select(QuestionAnswer, Job)
        .join(Application, Application.id == QuestionAnswer.application_id)
        .join(Job, Job.id == Application.job_id)
        .where(QuestionAnswer.needs_review.is_(True),
               Job.status.in_([JobStatus.MANUAL_ACTION_REQUIRED, JobStatus.AWAITING_APPROVAL, JobStatus.DRY_RUN,
                               JobStatus.FAILED, JobStatus.QUEUED]))
        .order_by(QuestionAnswer.id.desc()).limit(300)
    ).all()
    seen: set[str] = set()
    out = []
    for qa, job in rows:
        if qa.normalized_question in seen:
            continue
        seen.add(qa.normalized_question)
        remembered = db.scalar(select(AnswerMemory).where(AnswerMemory.normalized_question == qa.normalized_question))
        if remembered is not None:
            continue
        out.append(qa_dict(qa) | {"job_id": job.id, "job_title": job.title, "job_company": job.company})
    return out


class AnswerBody(BaseModel):
    question: str
    answer: str


@router.post("/answers/memory")
def save_answer(body: AnswerBody, db: Session = Depends(get_db)) -> dict[str, Any]:
    if not body.question.strip() or not body.answer.strip():
        raise HTTPException(400, "question and answer are required")
    row = remember_answer(db, body.question.strip(), body.answer.strip())
    return memory_dict(row)


@router.get("/answers/memory")
def list_memory(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return [memory_dict(m) for m in db.scalars(select(AnswerMemory).order_by(AnswerMemory.updated_at.desc()))]


@router.delete("/answers/memory/{memory_id}")
def delete_memory(memory_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    row = db.get(AnswerMemory, memory_id)
    if row is None:
        raise HTTPException(404, "Not found")
    db.delete(row)
    return {"ok": True}


@router.get("/ai/status")
async def ai_status() -> dict[str, Any]:
    return await get_provider().health(use_cache=False)


@router.get("/screenshots/{name}")
def screenshot(name: str) -> FileResponse:
    base = get_settings().screenshots_path.resolve()
    path = (base / Path(name).name).resolve()
    if path.parent != base or not path.is_file() or path.suffix not in {".png", ".html"}:
        raise HTTPException(404, "Not found")
    return FileResponse(path, media_type="image/png" if path.suffix == ".png" else "text/plain")
