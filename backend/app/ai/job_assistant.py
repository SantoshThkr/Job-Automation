"""AI job assistant: job understanding, factual question answering and short written answers.

Every output carries a confidence score. Low-confidence or ungrounded output is never used
automatically - callers route it to MANUAL_REVIEW.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from app.ai.guard import GuardResult, check_text
from app.ai.provider import AIResponseError, AIUnavailable, OllamaProvider, get_provider
from app.matching.analyzer import JobInput
from app.profile import Profile

log = logging.getLogger(__name__)

MAX_JD_CHARS = 6000


class JobUnderstanding(BaseModel):
    is_frontend_role: bool
    frontend_percentage: int = Field(ge=0, le=100)
    relevance_score: int = Field(ge=0, le=100)
    important_skills: list[str] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)
    recommendation: Literal["APPLY", "SKIP", "MANUAL_REVIEW"]
    confidence: float = Field(ge=0, le=1)
    summary: str = ""

    @field_validator("frontend_percentage", "relevance_score", mode="before")
    @classmethod
    def _clamp_int(cls, v):
        try:
            return max(0, min(100, int(round(float(v)))))
        except (TypeError, ValueError):
            raise ValueError("not a number") from None

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp_conf(cls, v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise ValueError("not a number") from None
        if f > 1:  # model answered in percent
            f = f / 100
        return max(0.0, min(1.0, f))

    @field_validator("recommendation", mode="before")
    @classmethod
    def _rec(cls, v):
        s = str(v).strip().upper().replace(" ", "_")
        return {"REVIEW": "MANUAL_REVIEW", "MANUAL": "MANUAL_REVIEW"}.get(s, s)


_UNDERSTANDING_SCHEMA = {
    "type": "object",
    "properties": {
        "is_frontend_role": {"type": "boolean"},
        "frontend_percentage": {"type": "integer"},
        "relevance_score": {"type": "integer"},
        "important_skills": {"type": "array", "items": {"type": "string"}},
        "concerns": {"type": "array", "items": {"type": "string"}},
        "recommendation": {"type": "string", "enum": ["APPLY", "SKIP", "MANUAL_REVIEW"]},
        "confidence": {"type": "number"},
        "summary": {"type": "string"},
    },
    "required": ["is_frontend_role", "frontend_percentage", "relevance_score", "important_skills",
                 "concerns", "recommendation", "confidence", "summary"],
}

_FACT_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "confidence": {"type": "number"},
        "profile_fields_used": {"type": "array", "items": {"type": "string"}},
        "needs_human": {"type": "boolean"},
    },
    "required": ["answer", "confidence", "profile_fields_used", "needs_human"],
}

_WRITE_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}, "confidence": {"type": "number"}},
    "required": ["answer", "confidence"],
}


@dataclass
class AIResult:
    answer: str | None
    confidence: float
    note: str = ""
    guard: GuardResult | None = None
    raw: dict = field(default_factory=dict)


def _job_block(job: JobInput) -> str:
    jd = (job.description or "")[:MAX_JD_CHARS]
    return (f"Title: {job.title}\nCompany: {job.company}\nLocation: {job.location}\n"
            f"Experience: {job.experience_text}\nKey skills: {', '.join(job.skills)}\n\nDescription:\n{jd}")


class JobAssistant:
    def __init__(self, provider: OllamaProvider | None = None) -> None:
        self.provider = provider or get_provider()

    async def available(self) -> bool:
        try:
            return await self.provider.is_available()
        except Exception:
            return False

    # ------------------------------------------------------------ jobs ---
    async def understand_job(self, profile: Profile, job: JobInput) -> JobUnderstanding:
        p = profile
        candidate = {
            "primary_skills": p.skills.primary,
            "secondary_skills": p.skills.secondary,
            "total_experience_years": p.professional.total_experience_years,
            "current_title": p.professional.current_title,
            "target_titles": p.job_preferences.target_titles[:6],
            "preferred_locations": p.work_preferences.preferred_locations,
            "open_to_remote": p.work_preferences.open_to_remote,
            "minimum_frontend_share": p.job_preferences.min_frontend_ratio,
            "accepts_fullstack": p.job_preferences.accept_fullstack,
        }
        system = ("You are a precise technical recruiter. You judge how well a job fits a frontend-focused "
                  "candidate. Answer ONLY with a JSON object matching the requested fields.")
        prompt = (
            f"CANDIDATE:\n{json.dumps(candidate, ensure_ascii=False)}\n\nJOB:\n{_job_block(job)}\n\n"
            "Return JSON with:\n"
            "- is_frontend_role: true if most of the daily work is frontend/UI engineering\n"
            "- frontend_percentage: 0-100 share of frontend work vs backend/devops/data/mobile\n"
            "- relevance_score: 0-100 fit for THIS candidate (skills, seniority, experience, location)\n"
            "- important_skills: up to 8 skills the job really requires\n"
            "- concerns: short list of mismatches (empty if none)\n"
            "- recommendation: APPLY, SKIP or MANUAL_REVIEW (use MANUAL_REVIEW when unsure)\n"
            "- confidence: 0.0-1.0 how sure you are\n"
            "- summary: one sentence"
        )
        data = await self.provider.chat_json(system, prompt, json_schema=_UNDERSTANDING_SCHEMA, max_tokens=600)
        try:
            return JobUnderstanding.model_validate(data)
        except ValidationError as exc:
            raise AIResponseError(f"invalid job analysis: {exc.errors()[:2]}") from exc

    # ------------------------------------------------------- questions ---
    async def answer_factual(self, profile: Profile, question: str, field_type: str = "text",
                             options: list[str] | None = None, job: JobInput | None = None) -> AIResult:
        """Answer a factual application question strictly from the profile."""
        facts = profile.facts_for_ai()
        system = (
            "You fill job application forms for a candidate. Use ONLY the facts in CANDIDATE_PROFILE. "
            "Never invent or estimate numbers, employers, degrees, dates, certifications, salaries or personal "
            "data. If the profile does not contain the answer, return needs_human=true, an empty answer and "
            "confidence 0. Answer with JSON only.")
        opt_txt = ""
        if options:
            opt_txt = ("\nThe answer MUST be exactly one of these options (copy it verbatim):\n"
                       + "\n".join(f"- {o}" for o in options[:40]))
        prompt = (
            f"CANDIDATE_PROFILE:\n{json.dumps(facts, ensure_ascii=False)}\n\n"
            f"QUESTION ({field_type}): {question}{opt_txt}\n\n"
            "Return JSON: answer (short), confidence (0-1), profile_fields_used (profile keys you relied on), "
            "needs_human (true if the profile does not contain the answer).")
        try:
            data = await self.provider.chat_json(system, prompt, json_schema=_FACT_SCHEMA, max_tokens=250)
        except AIResponseError as exc:
            return AIResult(None, 0.0, f"malformed AI response: {exc}")
        if not isinstance(data, dict):
            return AIResult(None, 0.0, "AI response was not an object")
        answer = str(data.get("answer") or "").strip()
        try:
            conf = float(data.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        conf = conf / 100 if conf > 1 else conf
        used = [u for u in (data.get("profile_fields_used") or []) if isinstance(u, str)]
        if data.get("needs_human") or not answer:
            return AIResult(None, 0.0, "AI: profile does not contain this information", raw=data)
        valid_keys = set(facts)
        grounded = [u for u in used if u.split(".")[0].split("[")[0] in valid_keys]
        if not grounded:
            conf = min(conf, 0.5)
        guard = check_text(answer, facts, "", allow_job_numbers=False)
        conf = min(conf, 0.85) * guard.score
        note = "AI answer grounded in: " + ", ".join(grounded) if grounded else "AI answer not grounded"
        if not guard.ok:
            note += "; guard: " + "; ".join(guard.violations)
        return AIResult(answer, round(conf, 3), note, guard, data)

    async def write_answer(self, profile: Profile, question: str, job: JobInput | None,
                           max_words: int = 120, max_chars: int | None = None) -> AIResult:
        """Write a concise free-text answer (e.g. 'Why are you interested in this role?')."""
        facts = profile.facts_for_ai()
        job_txt = _job_block(job) if job else "(no job description available)"
        system = (
            "You write short, sincere job application answers in first person for the candidate. Use ONLY facts "
            "from CANDIDATE_PROFILE about the candidate, and only details from JOB about the company/role. "
            "Do not invent employers, projects, metrics, numbers, degrees or skills. No greetings, no sign-off, "
            "no placeholders. Answer with JSON only.")
        prompt = (
            f"CANDIDATE_PROFILE:\n{json.dumps(facts, ensure_ascii=False)}\n\nJOB:\n{job_txt}\n\n"
            f"QUESTION: {question}\n\nWrite at most {max_words} words. Return JSON: answer, confidence (0-1, "
            "low if the profile lacks enough facts).")
        try:
            data = await self.provider.chat_json(system, prompt, json_schema=_WRITE_SCHEMA,
                                                 temperature=0.3, max_tokens=500)
        except AIResponseError as exc:
            return AIResult(None, 0.0, f"malformed AI response: {exc}")
        if not isinstance(data, dict):
            return AIResult(None, 0.0, "AI response was not an object")
        answer = re.sub(r"\s+", " ", str(data.get("answer") or "")).strip().strip('"')
        if not answer or re.search(r"\[[^\]]*\]|\{[^}]*\}|<[^>]+>|lorem ipsum", answer, re.I):
            return AIResult(None, 0.0, "AI answer empty or contains placeholders", raw=data)
        words = answer.split()
        if len(words) > max_words:
            answer = " ".join(words[:max_words]).rstrip(",;") + "."
        if max_chars and len(answer) > max_chars:
            answer = answer[:max_chars].rsplit(" ", 1)[0].rstrip(",;") + "."
        try:
            conf = float(data.get("confidence") or 0.6)
        except (TypeError, ValueError):
            conf = 0.6
        conf = conf / 100 if conf > 1 else conf
        guard = check_text(answer, facts, job.description + " " + job.title + " " + job.company if job else "",
                           profile_skills=profile.skills.all)
        conf = round(min(conf, 0.88) * guard.score, 3)
        note = "AI-written answer"
        if guard.violations or guard.warnings:
            note += "; guard: " + "; ".join(guard.violations + guard.warnings)
        return AIResult(answer, conf, note, guard, data)


__all__ = ["AIResult", "AIUnavailable", "AIResponseError", "JobAssistant", "JobUnderstanding"]
