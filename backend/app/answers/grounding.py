"""Claim-level grounding checks for generated answers.

The hallucination guard (ai/guard.py) catches invented numbers, names and skills. Small local models also
embellish in subtler ways: crediting a technology to the wrong employer ("Next.js at Infosys" when Next.js
was used elsewhere) or inflating the role ("I led ..."). These checks reject such drafts, and attribute
the answer to the profile facts it actually uses (instead of trusting the model's own citations).
"""

from __future__ import annotations

import re

from app.answers.intent import question_skills
from app.answers.knowledge import THEME_SKILLS, CandidateKnowledge
from app.matching.skills import canonicalize, extract_skills, skill_def

_LEADERSHIP = re.compile(
    r"\b(led|leading|lead\s+(?:a|the|development|developers|engineers)|managed|managing|mentor(?:ed|ing)?|"
    r"architect(?:ed|ing)|headed|heading|owned|spearhead(?:ed|ing)?|founded|directed|supervis(?:ed|ing)|"
    r"oversaw|overseeing|team\s+lead|tech(?:nical)?\s+lead|in\s+charge\s+of)\b", re.I)
_STEM = {"led": "lead", "leading": "lead", "managed": "manag", "managing": "manag", "mentored": "mentor",
         "mentoring": "mentor", "architected": "architect", "architecting": "architect", "headed": "head",
         "heading": "head", "owned": "own", "spearheaded": "spearhead", "spearheading": "spearhead",
         "founded": "found", "directed": "direct", "supervised": "supervis", "supervising": "supervis",
         "oversaw": "overs", "overseeing": "overs"}
_NEUTRAL_SKILLS = THEME_SKILLS | {"UI Development", "Browser APIs", "Backend Development"}


def _short_company(name: str) -> str:
    n = re.sub(r"\b(?:pvt|private|ltd|limited|llp|inc|corp|corporation|co)\b\.?", "", name or "", flags=re.I)
    return re.sub(r"[^a-z0-9& ]+", " ", n.lower()).split("&")[0].strip()


def check_claims(text: str, k: CandidateKnowledge) -> list[str]:
    """Problems in a generated answer that the guard does not see (empty list = acceptable)."""
    problems: list[str] = []
    corpus = k.corpus().lower()
    for m in _LEADERSHIP.finditer(text):
        word = m.group(1).lower().split()[0]
        stem = _STEM.get(word, word[:5])
        if not re.search(rf"\b{re.escape(stem)}", corpus):
            problems.append(f"claims '{m.group(1)}' (not in your profile)")
    roles = [(i, w) for i, w in enumerate(k.profile.professional.work_history) if w.company]
    for sentence in re.split(r"(?<=[.!?;])\s+", text):
        low = sentence.lower()
        mentioned = [(i, w) for i, w in roles if _short_company(w.company) and _short_company(w.company) in low]
        if not mentioned:
            continue
        allowed: set[str] = set()
        for _i, w in mentioned:
            allowed |= {canonicalize(t) for t in w.technologies} | set(extract_skills(w.summary))
        used = {s for s in question_skills(sentence) if (d := skill_def(s)) and not d.signal_only}
        extra = sorted(s for s in used - allowed - _NEUTRAL_SKILLS)
        if extra:
            names = ", ".join(w.company for _, w in mentioned)
            problems.append(f"credits {', '.join(extra)} to {names}")
    return problems


def attribute_sources(text: str, k: CandidateKnowledge, job_text: str = "") -> list[str]:
    """Profile keys whose content the answer uses (skills, employers, themes), plus the job posting."""
    low = text.lower()
    skills = set(question_skills(text))
    keys: list[str] = []
    for f in k.facts:
        hit = bool(f.skills & skills)
        if f.kind == "role":
            company = _short_company(f.text.split(" at ", 1)[-1].split("(")[0])
            hit = hit or (bool(company) and company in low)
        if hit:
            keys.append(f.key)
    years, ys = k.total_years_phrase()
    if years and years.split()[0].rstrip("+") in low:
        keys += ys
    if job_text and (set(extract_skills(job_text)) & skills):
        keys.append("job.description")
    return list(dict.fromkeys(keys))
