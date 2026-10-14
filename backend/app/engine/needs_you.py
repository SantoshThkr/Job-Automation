"""'Needs you' items: why automation stopped on a job, what exactly is missing, and what happens when you answer.

Every item is built from the recorded application (status reason, the questions the answer engine could not answer,
the open gate) and re-checked against your *current* profile and saved answers, so an item tells you whether the
profile can already answer it, whether a saved answer exists, and offers a suggestion only when that is safe.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.answers.answerer import Answerer, MemoryHit, canonical_key
from app.answers.intent import ASK_IF_MISSING, Category
from app.answers.options import choice_labels
from app.answers.question_bank import QuestionContext, normalize_question
from app.models import AgentState, AnswerMemory, Job, JobStatus, QuestionAnswer

# (kind, pattern on the stop reason, what it means for you)
STOP_KINDS: list[tuple[str, re.Pattern[str], str]] = [
    ("questions", re.compile(r"question|need your answer|needs your answers|needs your input|screening", re.I),
     "Required questions the assistant could not answer truthfully from your profile or saved answers"),
    ("profile", re.compile(r"profile incomplete|profile\.json|profile_confirmed", re.I),
     "Your profile is missing details needed to apply"),
    ("account", re.compile(r"sign in|sign-in|log in|login|create an account|account|password|IBMid", re.I),
     "The site requires you to sign in or create an account (never done without you)"),
    ("verification", re.compile(r"captcha|otp|verification|security check|verify your email", re.I),
     "A CAPTCHA / OTP / verification step only you can complete"),
    ("email", re.compile(r"takes applications by email", re.I),
     "This employer only accepts applications by email"),
    ("site_error", re.compile(r"error page|bad gateway|server error|returned an error", re.I),
     "The site failed while receiving the application - it may not have arrived"),
    ("not_registered", re.compile(r"did not register|nothing was sent", re.I),
     "The click did not go through - nothing was sent"),
    ("unconfirmed", re.compile(r"confirmation not verified|no confirmation|right after submitting|please verify", re.I),
     "Submitted - confirmation not verified: the site gave no evidence the assistant could check"),
    ("form_rejected", re.compile(r"\bsays:|rejected|form errors|validation", re.I),
     "The site rejected a value; it needs fixing in the form"),
    ("navigation", re.compile(r"could not tell how to continue|could not find|did not open|too many|took too long|"
                              r"only a .* form|not a job\s+application|unknown application platform|dialog", re.I),
     "The assistant could not find the next step of the application on this page"),
    ("review", re.compile(r"needs your review|awaiting|approve", re.I), "Waiting for your approval before submitting"),
]
_SENSITIVE = {c.value for c in ASK_IF_MISSING} | {Category.SALARY.value}
# A question that asks you to *decide* something (a preference, a commitment, a legal or pay-related statement) -
# as opposed to a fact about you that is merely missing from the profile (a date of birth, years with a tool).
DECISION_MEANING = ("A personal preference or a consequential answer (legal, pay, commitment) that only you can decide - "
                    "the assistant never decides these for you")
_DECISION_CATEGORIES = {Category.PREFERENCE.value, Category.LEGAL_DECLARATION.value, Category.VISA.value,
                        Category.DEMOGRAPHIC.value, Category.SALARY.value}
_DECISION_WORDS = re.compile(
    r"\b(?:are\s+you\s+(?:willing|open|comfortable|ok(?:ay)?|fine|happy|ready|able|interested)|would\s+you|will\s+you|"
    r"do\s+you\s+(?:agree|consent|accept|authori[sz]e|prefer|want|wish|require)|can\s+you\s+(?:join|relocate|work|commit|"
    r"start|travel)|i\s+(?:agree|consent|certify|acknowledge|confirm|authori[sz]e)|happy\s+with|acceptable|drug\s+test|"
    r"background\s+(?:check|verification)|sponsorship|relocat\w+|contract\s+(?:role|position)|bond|non[-\s]?compete)\b", re.I)


def is_decision(question: str, category: str = "", missing: str = "") -> bool:
    """The question asks for your decision rather than for a fact (see DECISION_MEANING)."""
    return (category in _DECISION_CATEGORIES or "work_preferences." in (missing or "")
            or bool(_DECISION_WORDS.search(question or "")))


def classify_stop(reason: str) -> tuple[str, str]:
    for kind, rx, meaning in STOP_KINDS:
        if rx.search(reason or ""):
            return kind, meaning
    return "other", "The assistant stopped and needs you to look at this application"


@dataclass
class SavedAnswer:
    answer: str
    scope: str


def saved_answer(db: Session, question: str, *, company: str = "", portal: str = "",
                 title: str = "") -> SavedAnswer | None:
    """Your saved answer that may be reused for this job: the most specific one that applies (this job, this
    employer, this portal, then your general answer), else the one saved under the question's wording-independent
    key ('skill years: mongodb', 'fact: date of birth')."""
    from app.engine.recorder import lookup_memory

    nq = normalize_question(question)
    hit: MemoryHit | None = lookup_memory(db, nq, company=company, portal=portal, title=title, count_use=False)
    if hit is None and (key := canonical_key(question)):
        row = db.scalar(select(AnswerMemory).where(AnswerMemory.normalized_question == key,
                                                   AnswerMemory.scope == "category"))
        if row is not None and row.answer:
            return SavedAnswer(row.answer, "category")
    return SavedAnswer(hit.answer, hit.scope) if hit is not None else None


def _profile_check(answerer: Answerer, qa: QuestionAnswer, job: Job) -> dict[str, Any]:
    """Ask the answer engine again with your current profile (no AI): can it answer now, and is a suggestion safe?"""
    import asyncio

    from app.matching.analyzer import JobInput

    ctx = QuestionContext(question=qa.question, field_type=qa.field_type or "text", options=list(qa.options or []),
                          required=True, job=JobInput(title=job.title, company=job.company, location=job.location or ""),
                          portal=job.portal)
    loop = asyncio.new_event_loop()  # API handlers run in worker threads: a private loop for this deterministic check
    try:
        ans = loop.run_until_complete(answerer.answer(ctx))
    except RuntimeError:  # called from inside a running event loop
        return {"profile_can_answer": False, "suggestion": None, "suggestion_note": "", "missing": qa.note or ""}
    finally:
        loop.close()
    can = ans.value not in (None, "") and not ans.needs_review and ans.mode not in {"skipped", "not_a_question"}
    suggestion, why = None, ""
    if can:
        suggestion, why = ans.value, f"from your profile ({ans.note or ans.mode})"
    elif ans.value not in (None, "") and ans.category not in _SENSITIVE:
        suggestion, why = ans.value, ans.note or "suggested from your profile - please confirm"
    elif ans.value is None and ans.yes_no is False and ans.category not in _SENSITIVE:
        suggestion, why = "No", ans.note
    return {"profile_can_answer": can, "suggestion": suggestion, "suggestion_note": why,
            "missing": "" if can else (ans.note or qa.note or ""), "category": ans.category or qa.category or "",
            "sensitive": (ans.category or qa.category or "") in _SENSITIVE}


def pending_questions(job: Job) -> list[QuestionAnswer]:
    app = job.application
    if app is None:
        return []
    seen: set[str] = set()
    out: list[QuestionAnswer] = []
    for qa in sorted(app.answers, key=lambda q: q.id):
        if not (qa.needs_review and qa.required) or qa.normalized_question in seen:
            continue
        seen.add(qa.normalized_question)
        out.append(qa)
    return out


def resume_plan(job: Job, live: bool) -> tuple[str, str]:
    app = job.application
    if live:
        return "live", ("The application is open in the browser right now: your answers are filled into the same "
                        "form and it continues from the same step.")
    if app is not None and app.resume_url and app.apply_target == "external":
        return "continue_url", ("The browser form from that attempt is closed. Retry reopens the company site where it "
                                "stopped and refills everything from your profile and saved answers.")
    if app is not None and app.submit_attempted and not app.confirmed:
        return "verify_first", ("It was submitted, but no confirmation could be verified. 'Retry verification' looks "
                                "again (the portal's Applied state, the submitted page) without submitting again; "
                                "'Mark as applied' if you can see it went through.")
    return "restart", ("The form from that attempt is closed. Retry starts this application again safely and fills "
                       "your saved answers automatically - nothing you already answered is asked again.")


def needs_you_items(db: Session, *, include_parked: bool = False) -> list[dict[str, Any]]:
    from app.profile import get_profile_store

    statuses = [JobStatus.MANUAL_ACTION_REQUIRED, JobStatus.AWAITING_APPROVAL]
    if include_parked:
        statuses.append(JobStatus.PARKED)
    jobs = db.scalars(select(Job).where(Job.status.in_(statuses)).order_by(Job.updated_at.desc())).all()
    state = AgentState.get(db)
    answerer = Answerer(get_profile_store().load(), None)
    items = []
    for job in jobs:
        app = job.application
        reason = (app.manual_action_reason if app is not None and app.manual_action_reason else "") or job.status_reason
        kind, meaning = classify_stop(reason)
        live = bool(state.gate_kind) and state.pending_job_id == job.id
        plan, plan_note = resume_plan(job, live)
        questions = []
        for qa in pending_questions(job):
            saved = saved_answer(db, qa.question, company=job.company, portal=job.portal, title=job.title)
            check = _profile_check(answerer, qa, job)
            cat = check.get("category") or qa.category or ""
            questions.append({
                "decision": is_decision(qa.question, cat, check["missing"]),
                "id": qa.id, "question": qa.question, "field_type": qa.field_type,
                "options": choice_labels(qa.options),
                "required": qa.required, "why": qa.note or "", "category": check.get("category") or qa.category or "",
                "profile_can_answer": check["profile_can_answer"], "missing": check["missing"],
                "saved_answer": saved.answer if saved else None, "saved_scope": saved.scope if saved else None,
                "suggestion": None if saved else check["suggestion"], "suggestion_note": check["suggestion_note"],
                "sensitive": check.get("sensitive", False),
            })
        if questions and kind not in {"questions", "profile"}:
            kind, meaning = "questions", STOP_KINDS[0][2]
        still_open = [q for q in questions if not q["saved_answer"] and not q["profile_can_answer"]]
        if kind == "questions" and still_open and all(q["decision"] for q in still_open):
            kind, meaning = "decision", DECISION_MEANING  # shown as "Needs decision": nothing is missing but your call
        items.append({
            "job_id": job.id, "company": job.company, "title": job.title, "portal": job.portal, "url": job.url,
            "ats": (app.ats_platform if app is not None else "") or "", "status": job.status,
            "step": (app.last_step if app is not None else "") or "", "problem": reason, "stop_kind": kind,
            "why": meaning, "live": live, "resume": plan, "resume_note": plan_note,
            "submit_attempted": bool(app is not None and app.submit_attempted),
            "verification": (app.verification_state if app is not None else "") or "",
            "evidence": list(app.verification_evidence or []) if app is not None else [],
            "final_url": (app.final_url if app is not None else "") or "", "updated_at": job.updated_at.isoformat()
            if job.updated_at else None, "questions": questions,
            "unanswered": sum(1 for q in questions if not q["saved_answer"] and not q["profile_can_answer"]),
        })
    return items


def blocking_questions_answered(db: Session, job: Job, answerer: Answerer | None = None) -> bool:
    """Every question that stopped this job now has a saved answer (usable for this employer / portal) or is
    answered by your current profile."""
    from app.profile import get_profile_store

    pending = pending_questions(job)
    if not pending:
        return False
    answerer = answerer or Answerer(get_profile_store().load(), None)
    for qa in pending:
        if saved_answer(db, qa.question, company=job.company, portal=job.portal, title=job.title) is not None:
            continue
        if not _profile_check(answerer, qa, job)["profile_can_answer"]:
            return False
    return True
