"""Deterministic relevance scoring and APPLY / SKIP / MANUAL_REVIEW classification."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from rapidfuzz import fuzz

from app.matching.analyzer import (
    SENIORITY_RANK,
    JobAnalysis,
    JobInput,
    analyze_job,
    normalize_city,
    seniority_for_years,
)
from app.matching.skills import canonicalize, skill_def
from app.models import Decision
from app.profile import Profile

WEIGHTS = {"title": 0.25, "frontend_ratio": 0.20, "skills": 0.30, "experience": 0.12,
           "seniority": 0.08, "location": 0.05}

_IMPORTANCE = {"frontend": 1.0, "backend": 1.0, "mobile": 1.0, "data": 1.0, "fullstack_js": 0.8,
               "testing": 0.6, "devops": 0.5, "general": 0.4, "design": 0.4}


@dataclass
class MatchResult:
    score: float
    decision: str
    rule_score: float
    breakdown: dict[str, float]
    frontend_ratio: float | None
    category: str
    matched_skills: list[str] = field(default_factory=list)
    missing_skills: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    analysis: dict[str, Any] = field(default_factory=dict)
    ai: dict[str, Any] | None = None
    resume_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def keyword_in(keyword: str, text: str) -> bool:
    kw = keyword.strip().lower()
    if not kw:
        return False
    left = r"(?<![a-z0-9])" if kw[0].isalnum() else ""
    right = r"(?![a-z0-9])" if kw[-1].isalnum() else ""
    return re.search(left + re.escape(kw) + right, text.lower()) is not None


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9+#. ]+", " ", (s or "").lower()).strip()


def _profile_skill_map(profile: Profile) -> dict[str, str]:
    out: dict[str, str] = {}
    for s in profile.skills.secondary:
        out[canonicalize(s)] = "secondary"
    for s in profile.skills.primary:
        out[canonicalize(s)] = "primary"
    return out


class _Ctx:
    def __init__(self) -> None:
        self.blockers: list[str] = []
        self.flags: list[str] = []  # anything here forces MANUAL_REVIEW instead of APPLY
        self.reasons: list[str] = []


def _score_title(profile: Profile, job: JobInput, a: JobAnalysis, ctx: _Ctx) -> float:
    prefs = profile.job_preferences
    title = _norm(job.title)
    for kw in prefs.excluded_title_keywords:
        if keyword_in(kw, job.title):
            ctx.blockers.append(f"Title contains excluded keyword '{kw.strip()}'")
            return 0.0
    best_target = max((fuzz.token_set_ratio(title, _norm(t)) for t in prefs.target_titles), default=0)
    best_accept = max((fuzz.token_set_ratio(title, _norm(t)) for t in prefs.acceptable_titles), default=0)
    cat = a.title_category
    if cat == "fullstack":
        if prefs.accept_fullstack:
            ctx.reasons.append(f"Title '{job.title}' is full-stack (accepted)")
            return 80.0
        ctx.reasons.append(f"Title '{job.title}' is full-stack (not preferred)")
        return 30.0
    if cat == "frontend" or (best_target >= 92 and cat not in {"backend", "mobile", "devops", "data", "qa"}):
        ctx.reasons.append(f"Title '{job.title}' is a frontend role")
        return 100.0
    if cat in {"backend", "mobile", "devops", "data", "qa"}:
        ctx.reasons.append(f"Title '{job.title}' looks like a {cat} role")
        return 10.0
    if best_accept >= 90:
        ctx.reasons.append(f"Title '{job.title}' matches an acceptable title")
        return 75.0
    if cat == "generic":
        ctx.reasons.append(f"Title '{job.title}' is generic; relying on the JD")
        return 55.0
    ctx.flags.append(f"Unclear role title '{job.title}'")
    return 30.0


def _score_ratio(profile: Profile, a: JobAnalysis, ctx: _Ctx) -> float:
    ratio = a.frontend_ratio
    min_ratio = profile.job_preferences.min_frontend_ratio
    if ratio is None:
        ctx.flags.append("No clear frontend/backend signal in the job description")
        return 50.0
    pct = round(ratio * 100)
    if ratio >= 0.7:
        score = 100.0
    elif ratio >= min_ratio:
        score = 75 + (ratio - min_ratio) / max(0.7 - min_ratio, 0.01) * 25
    elif ratio >= min_ratio - 0.2:
        score = 40 + (ratio - (min_ratio - 0.2)) / 0.2 * 35
    else:
        score = ratio / max(min_ratio - 0.2, 0.01) * 40
    ctx.reasons.append(f"Frontend focus ≈ {pct}% (your minimum {round(min_ratio * 100)}%)")
    if a.title_category == "fullstack" and ratio < min_ratio:
        ctx.flags.append(f"Full-stack role is backend-heavy (frontend ≈ {pct}%)")
    if a.title_category in {"generic", "other"} and ratio < min_ratio:
        ctx.flags.append(f"Generic title and frontend share is only ≈ {pct}%")
    return max(0.0, min(100.0, score))


def _score_skills(profile: Profile, job: JobInput, a: JobAnalysis, ctx: _Ctx,
                  out_matched: list[str], out_missing: list[str]) -> float:
    pskills = _profile_skill_map(profile)
    jd_skills = {s: n for s, n in a.skills.items() if (d := skill_def(s)) and not d.signal_only}
    total = matched = 0.0
    missing_required: list[str] = []
    for s in jd_skills:
        d = skill_def(s)
        imp = _IMPORTANCE.get(d.category if d else "general", 0.5)
        w = imp * (2.0 if s in a.required_skills else 1.0)
        total += w
        level = pskills.get(s)
        if level:
            matched += w * (1.0 if level == "primary" else 0.8)
            out_matched.append(s)
        elif s in a.required_skills:
            missing_required.append(s)
    # profile skills outside the taxonomy (e.g. "Shopify") matched literally
    jd_text = f"{job.title}\n{job.description}\n{' '.join(job.skills)}"
    for s, level in pskills.items():
        if skill_def(s) is None and keyword_in(s, jd_text):
            total += 1.0
            matched += 1.0 if level == "primary" else 0.8
            out_matched.append(s)
    out_missing.extend(missing_required)

    if total == 0:
        ctx.flags.append("Few recognizable skills in the job description")
        return 50.0
    coverage = matched / total

    jd_fw = a.frameworks
    cand_fw = {s for s in pskills if (d := skill_def(s)) and d.framework}
    if jd_fw:
        fw_fit = 1.0 if jd_fw & cand_fw else 0.0
        if not fw_fit:
            ctx.flags.append(f"Framework mismatch: JD wants {', '.join(sorted(jd_fw))}; "
                             f"profile has {', '.join(sorted(cand_fw)) or 'none'}")
    else:
        fw_fit = 0.7
    score = 100 * (0.65 * coverage + 0.35 * fw_fit)
    txt = f"Skills matched {len(out_matched)}/{len(jd_skills)}"
    if out_matched:
        txt += f": {', '.join(out_matched[:8])}"
    if missing_required:
        txt += f"; missing required: {', '.join(missing_required[:6])}"
    ctx.reasons.append(txt)
    return max(0.0, min(100.0, score))


def _score_experience(profile: Profile, a: JobAnalysis, ctx: _Ctx) -> float:
    years = profile.professional.total_experience_years
    tol = profile.matching.experience_tolerance_years
    lo, hi = a.experience_min, a.experience_max
    if years is None:
        ctx.flags.append("Your total experience is not set in profile.json")
        return 60.0
    if lo is None and hi is None:
        ctx.reasons.append("Experience requirement not stated")
        return 70.0
    lo = lo or 0.0
    req = f"{lo:g}-{hi:g}" if hi is not None else f"{lo:g}+"
    if years < lo:
        gap = lo - years
        if gap > tol:
            ctx.blockers.append(f"Requires {req} years; you have {years:g}")
            return 0.0
        ctx.reasons.append(f"Experience: requires {req} yrs, you have {years:g} (slightly under)")
        return 100 - gap / max(tol, 0.5) * 40
    if hi is not None and years > hi:
        over = years - hi
        if hi <= 3 and hi <= years / 2:
            ctx.blockers.append(f"Role targets {req} years of experience; you have {years:g}")
            return 10.0
        ctx.reasons.append(f"Experience: requires {req} yrs, you have {years:g} (above range)")
        if over <= tol:
            return 85.0
        return 60.0 if over <= 2 * tol else 35.0
    ctx.reasons.append(f"Experience: requires {req} yrs, you have {years:g}")
    return 100.0


def _score_seniority(profile: Profile, a: JobAnalysis, ctx: _Ctx) -> float:
    cand = seniority_for_years(profile.professional.total_experience_years)
    job = a.title_seniority
    if job == "intern":
        ctx.blockers.append("Internship/trainee role")
        return 0.0
    if job == "director":
        ctx.blockers.append("Director/VP/head-level role")
        return 0.0
    if job == "manager":
        ctx.flags.append("People-management role")
        return 35.0
    if job == "unspecified":
        return 90.0
    diff = abs(SENIORITY_RANK[job] - SENIORITY_RANK[cand])
    score = {0: 100.0, 1: 80.0, 2: 45.0}.get(diff, 15.0)
    ctx.reasons.append(f"Seniority: {job} role vs your level ({cand})")
    return score


def _score_location(profile: Profile, job: JobInput, a: JobAnalysis, ctx: _Ctx) -> float:
    wp = profile.work_preferences
    prefs = [p for p in wp.preferred_locations if p.strip()]
    pref_cities = {c for p in prefs if (c := normalize_city(p))}
    wants_remote = wp.open_to_remote or any(p.lower() == "remote" for p in prefs)
    locs, mode = a.locations, a.work_mode
    if not prefs:
        return 80.0
    if "remote" in locs or mode == "remote":
        if wants_remote:
            ctx.reasons.append("Location: remote role")
            return 100.0
        ctx.reasons.append("Location: remote role (you prefer office)")
        return 50.0
    matched = [loc for loc in locs if loc in pref_cities]
    if matched:
        if mode == "onsite" and not wp.open_to_onsite:
            ctx.flags.append("On-site role but you are not open to on-site work")
            return 40.0
        if mode == "hybrid" and not wp.open_to_hybrid:
            ctx.flags.append("Hybrid role but you are not open to hybrid work")
            return 50.0
        ctx.reasons.append(f"Location: {matched[0].title()} matches your preference")
        return 100.0
    if "anywhere" in locs:
        ctx.reasons.append("Location: multiple / pan-India locations")
        return 85.0
    if not locs:
        ctx.reasons.append("Location not stated")
        return 65.0
    if wp.willing_to_relocate:
        ctx.reasons.append(f"Location: {job.location} (you are open to relocation)")
        return 60.0
    if profile.matching.location_strict:
        ctx.blockers.append(f"Location '{job.location}' is not in your preferred locations")
        return 0.0
    ctx.flags.append(f"Location '{job.location}' is not in your preferred locations")
    return 20.0


def evaluate_rules(profile: Profile, job: JobInput, analysis: JobAnalysis | None = None) -> MatchResult:
    a = analysis or analyze_job(job)
    ctx = _Ctx()
    matched: list[str] = []
    missing: list[str] = []

    prefs = profile.job_preferences
    for company in prefs.excluded_companies:
        if company.strip() and company.strip().lower() in (job.company or "").lower():
            ctx.blockers.append(f"Company '{job.company}' is excluded")
    for kw in prefs.excluded_jd_keywords:
        if keyword_in(kw, job.description):
            ctx.blockers.append(f"Job description contains excluded keyword '{kw}'")

    breakdown = {
        "title": _score_title(profile, job, a, ctx),
        "frontend_ratio": _score_ratio(profile, a, ctx),
        "skills": _score_skills(profile, job, a, ctx, matched, missing),
        "experience": _score_experience(profile, a, ctx),
        "seniority": _score_seniority(profile, a, ctx),
        "location": _score_location(profile, job, a, ctx),
    }
    if prefs.min_salary_lpa and a.salary_max_lpa is not None and a.salary_max_lpa < prefs.min_salary_lpa:
        ctx.blockers.append(f"Salary up to {a.salary_max_lpa:g} LPA is below your minimum {prefs.min_salary_lpa:g} LPA")
    if a.jd_length < 150 and a.title_category != "frontend":
        ctx.flags.append("Very short job description")

    score = round(sum(breakdown[k] * w for k, w in WEIGHTS.items()), 1)
    m = profile.matching
    if ctx.blockers:
        decision = Decision.SKIP
    elif score < m.skip_threshold:
        decision = Decision.SKIP
    elif score >= m.apply_threshold and not ctx.flags:
        decision = Decision.APPLY
    else:
        decision = Decision.MANUAL_REVIEW

    category = a.title_category if a.title_category in {"frontend", "fullstack", "backend"} else (
        "frontend" if (a.frontend_ratio or 0) >= 0.6 else "fullstack")
    return MatchResult(
        score=score, decision=decision.value, rule_score=score,
        breakdown={k: round(v, 1) for k, v in breakdown.items()},
        frontend_ratio=None if a.frontend_ratio is None else round(a.frontend_ratio, 2),
        category=category, matched_skills=matched, missing_skills=missing,
        blockers=ctx.blockers, flags=ctx.flags, reasons=ctx.reasons, analysis=a.to_dict(),
    )
