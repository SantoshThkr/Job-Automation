"""Model -> JSON helpers for the API."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from app.answers.options import choice_labels
from app.models import AgentState, AnswerMemory, Application, ApplicationEvent, Job, QuestionAnswer, Run


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") + "Z" if dt else None


def screenshot_url(path: str | None) -> str | None:
    return f"/api/screenshots/{Path(path).name}" if path else None


def posting_fields(job: Job, window=None) -> dict[str, Any]:
    """When the job was posted (never when it was found), how old it is, and what the job-recency window says."""
    from app.matching.posting import age_days, describe, listing_closed, posted_of, utcnow, verdict

    posted = posted_of(job)
    now = utcnow()
    out: dict[str, Any] = {
        "posted_at": iso(posted.at) if posted else None,
        "posted_label": describe(posted, now=now) if posted else "",
        "posted_age_days": round(age_days(posted, now=now), 1) if posted else None,
        "posted_date_only": bool(posted.date_only) if posted else False,
        "posted_open_ended": bool(posted.open_ended) if posted else False,
        "posted_source": posted.source if posted else "",
        "closed_reason": listing_closed(job, now=now),
    }
    if window is not None:
        state, why = verdict(posted, window, now=now)
        out.update({"recency": state, "recency_reason": why})
    return out


def job_summary(job: Job, window=None) -> dict[str, Any]:
    app = job.application
    return {
        **posting_fields(job, window),
        "id": job.id,
        "portal": job.portal,
        "title": job.title,
        "company": job.company,
        "location": job.location,
        "url": job.url,
        "match_score": job.match_score,
        "decision": job.decision,
        "status": job.status,
        "status_reason": job.status_reason,
        "apply_type": job.apply_type,
        "discovered_at": iso(job.discovered_at),
        "updated_at": iso(job.updated_at),
        "resume_id": (app.resume_id if app and app.resume_id else job.resume_id),
        "applied_at": iso(app.applied_at) if app else None,
        "ats_platform": app.ats_platform if app else None,
        "frontend_ratio": (job.match_details or {}).get("frontend_ratio"),
    }


def qa_dict(qa: QuestionAnswer) -> dict[str, Any]:
    return {
        "id": qa.id, "question": qa.question, "field_type": qa.field_type, "options": choice_labels(qa.options),
        "required": qa.required, "answer": qa.answer, "confidence": qa.confidence, "source": qa.source,
        "needs_review": qa.needs_review, "note": qa.note, "category": qa.category or "", "mode": qa.mode or "",
        "sources": qa.sources or [], "verified": qa.verified,
    }


def event_dict(ev: ApplicationEvent) -> dict[str, Any]:
    return {
        "id": ev.id, "job_id": ev.job_id, "ts": iso(ev.ts), "level": ev.level, "type": ev.type,
        "message": ev.message, "data": ev.data or {}, "screenshot": screenshot_url(ev.screenshot),
    }


def application_dict(app: Application | None) -> dict[str, Any] | None:
    if app is None:
        return None
    return {
        "id": app.id, "status": app.status, "mode": app.mode, "apply_target": app.apply_target,
        "ats_platform": app.ats_platform, "resume_id": app.resume_id, "resume_path": app.resume_path,
        "attempts": app.attempts, "last_step": app.last_step, "submit_attempted": app.submit_attempted,
        "confirmed": app.confirmed, "confirmation_text": app.confirmation_text,
        "failure_reason": app.failure_reason, "manual_action_reason": app.manual_action_reason,
        "review_decision": app.review_decision, "final_url": app.final_url, "resume_url": app.resume_url or "",
        "steps": app.steps or 0,
        "started_at": iso(app.started_at), "finished_at": iso(app.finished_at), "applied_at": iso(app.applied_at),
        "answers": [qa_dict(q) for q in app.answers],
    }


def job_detail(job: Job, window=None) -> dict[str, Any]:
    d = job_summary(job, window)
    d.update({
        "external_id": job.external_id,
        "experience_text": job.experience_text,
        "salary_text": job.salary_text,
        "posted_text": job.posted_text,
        "description": job.description,
        "skills": job.skills or [],
        "external_url": job.external_url,
        "search_query": job.search_query,
        "match_details": job.match_details or {},
        "evaluated_at": iso(job.evaluated_at),
        "application": application_dict(job.application),
        "events": [event_dict(e) for e in job.events],
    })
    return d


def agent_dict(st: AgentState, alive: bool, current_job: Job | None = None) -> dict[str, Any]:
    return {
        "desired_state": st.desired_state, "actual_state": st.actual_state if alive or st.actual_state in
        {"STOPPED", "IDLE", "ERROR"} else "STOPPED", "alive": alive, "mode": st.mode, "portals": st.portals or [],
        "pid": st.pid, "heartbeat_at": iso(st.heartbeat_at), "started_at": iso(st.started_at), "run_id": st.run_id,
        "current_activity": st.current_activity, "message": st.message, "last_error": st.last_error,
        "pending_job_id": st.pending_job_id, "gate_kind": st.gate_kind if alive else None,
        "gate_reason": st.gate_reason if alive else "", "gate_opened_at": iso(st.gate_opened_at),
        "gate_payload": (st.gate_payload or {}) if alive and st.gate_kind else {},
        "repeat_minutes": st.repeat_minutes,
        "recency": recency_dict(st.recency),
        "phase": (st.phase or "") if alive else "",
        "current_step": (st.current_step or "") if alive else "",
        "current_url": (st.current_url or "") if alive else "",
        "current_job": ({"id": current_job.id, "title": current_job.title, "company": current_job.company,
                         "portal": current_job.portal, "url": current_job.url, "status": current_job.status,
                         "match_score": current_job.match_score}
                        if alive and current_job is not None else None),
    }


def recency_dict(stored: dict | None) -> dict[str, Any]:
    """The job-recency window as the dashboard shows and edits it."""
    from app.matching.posting import CHOICES, window_from, window_to_dict

    window = window_from(stored)
    return {**window_to_dict(window), "label": window.label(), "chosen": bool(stored), "choices": list(CHOICES)}


def run_dict(run: Run) -> dict[str, Any]:
    return {"id": run.id, "mode": run.mode, "portals": run.portals or [], "status": run.status,
            "stats": run.stats or {}, "error": run.error, "started_at": iso(run.started_at),
            "finished_at": iso(run.finished_at)}


def memory_dict(m: AnswerMemory) -> dict[str, Any]:
    return {"id": m.id, "question": m.question, "answer": m.answer, "times_used": m.times_used,
            "updated_at": iso(m.updated_at), "scope": m.scope or "global", "company": m.company or "",
            "portal": m.portal or "", "job": m.job_key or "", "category": m.category or ""}
