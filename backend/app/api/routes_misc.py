"""Stats, events, answers memory, AI status, screenshots and health endpoints."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import Path as PathParam
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.ai.provider import get_provider
from app.api.serializers import event_dict, memory_dict, qa_dict
from app.config import SERVICE_NAME, get_settings
from app.db import get_db
from app.engine.needs_you import saved_answer
from app.engine.recorder import (add_event, forget_memory, local_day_start_utc, remember_answer, set_status,
                                 update_memory)
from app.answers.question_bank import normalize_question
from app.models import (AgentState, AnswerMemory, Application, ApplicationEvent, Decision, Job, JobStatus,
                        QuestionAnswer)

router = APIRouter(prefix="/api", tags=["misc"])

RowId = Annotated[int, PathParam(ge=1, le=2**31 - 1)]
QuestionText = Annotated[str, StringConstraints(min_length=1, max_length=2000)]
AnswerText = Annotated[str, StringConstraints(min_length=1, max_length=5000)]
ANSWER_SCOPES = {"auto", "global", "portal", "company", "job"}  # where a saved answer may be reused


@router.get("/health")
def health(db: Session = Depends(get_db)) -> dict[str, Any]:
    db.scalar(select(func.count(Job.id)))
    return {"ok": True, "service": SERVICE_NAME}


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
        "expired": by_status.get(JobStatus.EXPIRED, 0),
        "review_pending": by_status.get(JobStatus.REVIEW_PENDING, 0),
        "needs_you": by_status.get(JobStatus.MANUAL_ACTION_REQUIRED, 0) + by_status.get(JobStatus.AWAITING_APPROVAL, 0),
        "today_applications": applied_today,
        "weekly_applications": applied_week,
        "by_status": by_status,
        "by_portal": by_portal,
    }


@router.get("/events")
def recent_events(limit: int = Query(50, ge=1, le=300), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = db.scalars(select(ApplicationEvent).order_by(ApplicationEvent.id.desc()).limit(limit)).all()
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
        if saved_answer(db, qa.question, company=job.company, portal=job.portal, title=job.title) is not None:
            continue  # (an answer saved for another employer / job does not answer it here)
        out.append(qa_dict(qa) | {"job_id": job.id, "job_title": job.title, "job_company": job.company})
    return out


class AnswerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: QuestionText
    answer: AnswerText
    # employer-specific questions are remembered for that employer only
    job_id: Annotated[int, Field(ge=1, le=2**31 - 1)] | None = None


@router.post("/answers/memory")
def save_answer(body: AnswerBody, db: Session = Depends(get_db)) -> dict[str, Any]:
    if not body.question.strip() or not body.answer.strip():
        raise HTTPException(400, "question and answer are required")
    job = db.get(Job, body.job_id) if body.job_id else None
    row = remember_answer(db, body.question.strip(), body.answer.strip(), company=job.company if job else "",
                          portal=job.portal if job else "", title=job.title if job else "")
    requeued = requeue_answered_jobs(db)
    return memory_dict(row) | {"requeued_jobs": requeued}


def requeue_answered_jobs(db: Session) -> list[int]:
    """Jobs parked only because of questions you have now answered (and never submitted) go back to the queue.
    Answers count with their reuse scope: an employer-specific answer re-queues only that employer's job."""
    from app.engine.needs_you import blocking_questions_answered

    out: list[int] = []
    jobs = db.scalars(select(Job).join(Application, Application.job_id == Job.id).where(
        Job.status == JobStatus.MANUAL_ACTION_REQUIRED, Application.submit_attempted.is_(False))).all()
    for job in jobs:
        if not blocking_questions_answered(db, job):
            continue
        set_status(db, job, JobStatus.QUEUED, "Your answers are saved - will apply on the next run")
        add_event(db, job.id, "requeued", "All pending questions answered - queued again automatically")
        out.append(job.id)
    return out


class NeedsYouAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: Annotated[str, StringConstraints(max_length=2000)]
    answer: Annotated[str, StringConstraints(max_length=5000)]
    scope: Annotated[str, StringConstraints(max_length=16)] = "auto"  # auto | global | portal | company | job


class NeedsYouAnswers(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answers: Annotated[list[NeedsYouAnswer], Field(max_length=100)]


@router.get("/needs-you")
def needs_you(include_parked: bool = False, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Everything waiting for you, each with the exact problem, what is missing and what happens when you answer."""
    from app.engine.needs_you import needs_you_items

    items = needs_you_items(db, include_parked=include_parked)
    parked = db.scalar(select(func.count(Job.id)).where(Job.status == JobStatus.PARKED)) or 0
    kinds: dict[str, int] = {}
    for it in items:
        kinds[it["stop_kind"]] = kinds.get(it["stop_kind"], 0) + 1
    return {"items": items, "total": len(items), "parked": parked, "by_kind": kinds}


@router.post("/needs-you/{job_id}/answers")
def answer_needs_you(job_id: RowId, body: NeedsYouAnswers, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Save your answers (with their reuse scope) and continue the application: straight into the open form when the
    agent is waiting on this job, otherwise the job is queued again once nothing else blocks it."""
    from app.engine.needs_you import resume_plan

    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    answers = [a for a in body.answers if a.question.strip() and a.answer.strip()]
    if not answers:
        raise HTTPException(400, "no answers given")
    if any(a.scope not in ANSWER_SCOPES for a in answers):
        raise HTTPException(400, "scope must be auto, global, portal, company or job")
    saved = []
    for a in answers:  # your explicit choice wins over the automatic scope; each scope keeps its own answer
        row = remember_answer(db, a.question.strip(), a.answer.strip(), company=job.company, portal=job.portal,
                              title=job.title, scope=a.scope)
        saved.append(memory_dict(row))
    st = AgentState.get(db)
    live = st.gate_kind == "questions" and st.pending_job_id == job.id
    if live:
        # the agent is waiting on this very form: hand the answers to it (matched to the open questions)
        open_qs = (st.gate_payload or {}).get("questions") or []
        by_q = {normalize_question(a.question): a.answer.strip() for a in answers}
        st.gate_answers = {str(q.get("id")): by_q[nq] for q in open_qs
                           if (nq := normalize_question(str(q.get("question", "")))) in by_q}
        st.gate_response = "approve"
        requeued: list[int] = []
    else:
        requeued = requeue_answered_jobs(db)
    db.flush()
    plan, note = resume_plan(job, live)
    return {"saved": saved, "live": live, "requeued": job.id in requeued, "requeued_jobs": requeued,
            "resume": plan, "resume_note": note, "status": job.status}


@router.get("/answers/memory")
def list_memory(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return [memory_dict(m) for m in db.scalars(select(AnswerMemory).order_by(AnswerMemory.updated_at.desc()))]


class MemoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: AnswerText


@router.put("/answers/memory/{memory_id}")
def correct_memory(memory_id: RowId, body: MemoryUpdate, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Correct a saved answer. The corrected value is what every later form gets (its reuse scope is unchanged);
    jobs waiting only for this answer are queued again."""
    row = db.get(AnswerMemory, memory_id)
    if row is None:
        raise HTTPException(404, "Not found")
    if not body.answer.strip():
        raise HTTPException(400, "answer is required (delete the saved answer to remove it)")
    update_memory(db, row, body.answer.strip())
    return memory_dict(row) | {"requeued_jobs": requeue_answered_jobs(db)}


@router.delete("/answers/memory/{memory_id}")
def delete_memory(memory_id: RowId, db: Session = Depends(get_db)) -> dict[str, Any]:
    row = db.get(AnswerMemory, memory_id)
    if row is None:
        raise HTTPException(404, "Not found")
    forget_memory(db, row)
    return {"ok": True}


@router.get("/ai/status")
async def ai_status() -> dict[str, Any]:
    return await get_provider().health(use_cache=False)


@router.get("/screenshots/{name}")
def screenshot(name: Annotated[str, PathParam(max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")]) -> FileResponse:
    """A capture the agent saved. Only plain file names inside the screenshots folder; a saved page (.html) is sent
    as text and as a download, so nothing a web site wrote can ever run in the dashboard's origin."""
    base = get_settings().screenshots_path.resolve()
    path = (base / Path(name).name).resolve()
    if path.parent != base or not path.is_file() or path.is_symlink() or path.suffix not in {".png", ".html"}:
        raise HTTPException(404, "Not found")
    if path.suffix == ".png":
        return FileResponse(path, media_type="image/png")
    return FileResponse(path, media_type="text/plain; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{path.name}.txt"'})
