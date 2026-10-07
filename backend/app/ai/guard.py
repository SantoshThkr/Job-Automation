"""Hallucination guard: checks AI-written text against the facts it was allowed to use."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.matching.skills import all_skills, canonicalize, extract_skills, skill_def

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "twenty": 20,
}

_COMMON_CAPS = {
    "i", "i'm", "i've", "i'd", "i'll", "my", "me", "the", "this", "that", "these", "those", "as", "in", "with",
    "at", "your", "you", "we", "our", "it", "its", "over", "during", "having", "thank", "thanks", "regards",
    "dear", "hiring", "manager", "team", "hello", "hi", "also", "and", "but", "for", "from", "on", "to", "of",
    "if", "when", "while", "there", "here", "additionally", "moreover", "furthermore", "currently", "recently",
    "previously", "most", "best", "sincerely", "looking", "excited", "passionate", "experienced", "strong",
    "senior", "lead", "engineer", "engineering", "developer", "development", "frontend", "front-end",
    "full-stack", "fullstack", "backend", "web", "ui", "ux", "api", "apis", "role", "position", "company",
    "yes", "no", "not", "a", "an", "am", "is", "are", "be", "have", "has", "can", "will", "would", "should",
    "india", "remote", "hybrid", "lpa", "inr", "years", "year", "months", "days", "immediately", "immediate",
    "english", "javascript", "typescript", "css", "html", "spa", "ssr", "seo", "saas", "b2b", "b2c", "it",
    "software", "product", "products", "platform", "technology", "technologies", "applications", "application",
    "skills", "experience", "opportunity", "mission", "culture", "users", "customers", "solutions", "modern",
    "scalable", "high-quality", "performant", "responsive", "accessible", "react-based", "what", "why",
    "how", "which", "who", "where", "after", "before", "since", "so", "because", "thus", "therefore",
}


@dataclass
class GuardResult:
    ok: bool
    score: float
    violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _corpus_text(*parts: Any) -> str:
    out = []
    for p in parts:
        if p is None:
            continue
        out.append(p if isinstance(p, str) else json.dumps(p, ensure_ascii=False, default=str))
    return "\n".join(out).lower()


def _numbers(text: str) -> set[str]:
    nums = set()
    for m in re.finditer(r"(?<![\w.])(\d+(?:\.\d+)?)", text):
        n = m.group(1)
        nums.add(n.rstrip("0").rstrip(".") if "." in n else n)
    for word, val in _NUMBER_WORDS.items():
        if re.search(rf"\b{word}\b", text, re.I):
            nums.add(str(val))
    return nums


def check_text(answer: str, profile_facts: dict[str, Any], job_text: str = "", *,
               allow_job_numbers: bool = True, profile_skills: list[str] | None = None) -> GuardResult:
    """Flag numbers, names and skills in ``answer`` that are not supported by the inputs."""
    violations: list[str] = []
    warnings: list[str] = []
    facts_text = _corpus_text(profile_facts)
    job_low = (job_text or "").lower()
    allowed_corpus = facts_text + "\n" + (job_low if allow_job_numbers else "")

    allowed_numbers = _numbers(allowed_corpus)
    for n in _numbers(answer):
        if n not in allowed_numbers and n not in {"1", "0", "100"}:
            violations.append(f"number '{n}' is not in the profile")

    # Capitalised tokens (not at sentence start) that appear nowhere in profile/job/skill names
    known = facts_text + "\n" + job_low + "\n" + " ".join(s.name.lower() for s in all_skills())
    sentences = re.split(r"(?<=[.!?])\s+|\n+", answer)
    unknown: list[str] = []
    for sent in sentences:
        tokens = re.findall(r"[A-Za-z][A-Za-z0-9.&+#'-]*", sent)
        for i, tok in enumerate(tokens):
            if i == 0 or not tok[0].isupper():
                continue
            low = re.sub(r"['’]s$", "", tok.lower()).strip(".'’")
            if low in _COMMON_CAPS or low in known or len(low) <= 1:
                continue
            if skill_def(canonicalize(tok)):
                continue
            unknown.append(tok)
    for tok in dict.fromkeys(unknown):
        violations.append(f"name '{tok}' is not in the profile or job description")

    # Skills the answer claims that the candidate does not list
    if profile_skills is not None:
        mine = {canonicalize(s) for s in profile_skills} | set(extract_skills(facts_text))
        jd_skills = set(extract_skills(job_text))
        for s in extract_skills(answer):
            d = skill_def(s)
            if d is None or d.signal_only or s in mine:
                continue
            if s in jd_skills:
                warnings.append(f"mentions job skill '{s}' that is not in your profile")
            else:
                violations.append(f"mentions skill '{s}' that is not in your profile")

    score = max(0.0, 1.0 - 0.35 * len(violations) - 0.1 * len(warnings))
    return GuardResult(ok=not violations, score=score, violations=violations, warnings=warnings)
