"""Structured reason codes for every decision: a stable code, its kind (blocker = never applied to on its own score,
flag = yours to decide, status = the workflow set the job aside) and the human-readable text it came from.

The codes are derived from the wording the rules already produce, so a decision and its explanation never disagree;
a message no pattern knows gets ``OTHER`` (the dashboard still shows its text)."""

from __future__ import annotations

import re
from typing import Any

# (code, what it means, pattern on the rule's own message)
CODES: list[tuple[str, str, re.Pattern[str]]] = [(code, meaning, re.compile(rx, re.I)) for code, meaning, rx in [
    ("TITLE_EXCLUDED_KEYWORD", "the title contains a keyword you excluded", r"^Title contains excluded keyword"),
    ("TITLE_OTHER_FAMILY", "the title names another kind of role (backend, data, QA, DevOps, mobile)",
     r"^Title (?:looks like an? \w+|names an? (?:backend|mobile|devops|data|qa)) role"),
    ("SENIORITY", "the level does not fit (intern, director, junior for a senior)",
     r"^Internship/trainee|^Director/VP|^Junior-level role|^Seniority"),
    ("FRONTEND_SHARE_LOW", "too little frontend work in the description", r"^Not a frontend role|^Generic title and frontend share"),
    ("FULLSTACK_BACKEND_HEAVY", "a full-stack role whose backend outweighs the frontend",
     r"^Backend-heavy full-stack|^Full-stack role is backend-heavy|full-stack title, but"),
    ("FOREIGN_STACK", "mandatory skills of a stack you do not list",
     r"^The description requires .* not among your skills|^Generic title; the description centres on"),
    ("FRAMEWORK_MISMATCH", "another frontend framework than yours", r"^Framework mismatch"),
    ("EXPERIENCE_ABOVE", "asks for more years than you have", r"^Requires \d"),
    ("EXPERIENCE_BELOW", "targets far fewer years than you have", r"^Role targets"),
    ("LOCATION", "the location is not one you chose", r"^Location"),
    ("SALARY", "the salary is below your minimum", r"^Salary"),
    ("CMS_ROLE", "a CMS site-building role (WordPress, Shopify ...)", r"^CMS"),
    ("PLATFORM_ROLE", "customising a vendor platform (ServiceNow, Sitecore ...)", r"^Platform-specific role"),
    ("DESIGN_ROLE", "a designer's role", r"^Title names a design role"),
    ("SPECIALTY_ROLE", "a specialist role your profile does not list (blockchain, games ...)", r"^A specialist role"),
    ("EMPLOYMENT_TERMS", "freelance / contract / part-time / hourly / night-shift terms", r"^Employment terms"),
    ("THIN_EVIDENCE", "the posting says too little to judge",
     r"^Few recognizable skills|^Very short job description|^No clear frontend/backend signal|^Unclear role title"),
    ("EXCLUDED_COMPANY", "a company you excluded", r"^Company '.*' is excluded"),
    ("EXCLUDED_KEYWORD", "the description contains a keyword you excluded", r"^Job description contains excluded keyword"),
    ("AI_DISAGREES", "the local model read the description as not a fit", r"^AI disagrees"),
    ("AI_UNSURE", "the local model was unsure", r"^AI is unsure"),
]]
MEANING = {code: meaning for code, meaning, _rx in CODES} | {
    "OTHER": "another reason (see its text)",
    "TOO_OLD": "posted outside your job-recency window", "DATE_UNKNOWN": "posting date unknown",
    "CLOSED": "the listing is closed or expired", "DUPLICATE": "the same role already handled elsewhere",
    "ALREADY_APPLIED": "the portal shows it as applied", "ERROR": "the page could not be read",
    "SKIPPED_BY_YOU": "you skipped it", "LOW_SCORE": "the overall fit is below your skip threshold",
}


def code_of(text: str) -> str:
    for code, _meaning, rx in CODES:
        if rx.search(text or ""):
            return code
    return "OTHER"


def codes_for(blockers: list[str], flags: list[str]) -> list[dict[str, str]]:
    return ([{"code": code_of(t), "kind": "blocker", "text": t} for t in blockers]
            + [{"code": code_of(t), "kind": "flag", "text": t} for t in flags])


def decided_by(result: Any) -> str:
    """'rules' (deterministic), 'rules+ai' (the local model's reading was used) - and 'uncertain' when the decision
    is left to you."""
    ai = getattr(result, "ai", None) or {}
    who = "rules+ai" if ai.get("used") else "rules"
    return f"{who} (uncertain)" if getattr(result, "decision", "") == "MANUAL_REVIEW" else who


_STATUS_CODES = [
    ("TOO_OLD", r"outside your job-recency window|before your date range|after your date range"),
    ("DATE_UNKNOWN", r"posting date unknown|precise only to"),
    ("CLOSED", r"^Closed:|no longer accepting|closed|expired"),
    ("DUPLICATE", r"^Same role already handled"),
    ("ALREADY_APPLIED", r"shows this job as applied"),
    ("ERROR", r"^Error:"),
    ("SKIPPED_BY_YOU", r"^Skipped by you"),
]


def status_code(status: str, reason: str, match_details: dict | None = None) -> str:
    """The one code that says why a job ended where it is: the workflow's own reason (age, closed, duplicate ...)
    first, else the first blocker / flag of its match, else LOW_SCORE for a plain low score."""
    for code, rx in _STATUS_CODES:
        if re.search(rx, reason or "", re.I):
            return code
    details = match_details or {}
    first = [*(details.get("blockers") or []), *(details.get("flags") or [])]
    if first:
        return code_of(first[0])
    if status == "SKIPPED":
        return "LOW_SCORE" if details else code_of(reason)
    return ""
