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
    normalize_title_aliases,
    seniority_for_years,
    title_backend_stack,
)
from app.matching.skills import canonicalize, skill_def
from app.models import Decision
from app.profile import Profile

WEIGHTS = {"title": 0.25, "frontend_ratio": 0.20, "skills": 0.30, "experience": 0.12,
           "seniority": 0.08, "location": 0.05}

_IMPORTANCE = {"frontend": 1.0, "backend": 1.0, "mobile": 1.0, "data": 1.0, "fullstack_js": 0.8,
               "testing": 0.6, "devops": 0.5, "general": 0.4, "design": 0.4, "ai": 0.7}


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
    profile_rev: str = ""  # the preferences this decision was made with (see preferences_rev)
    ai_focus: bool = False  # frontend + AI (LLM / RAG / agents) role
    resume_category: str = "frontend"  # frontend | fullstack | ai: which resume flavour fits
    priority: str = ""  # human-readable preference note (React/Next.js first, Angular lower, ...)

    def to_dict(self) -> dict[str, Any]:
        from app.matching.reasons import codes_for, decided_by

        d = asdict(self)
        d["codes"] = codes_for(self.blockers, self.flags)  # structured: code, blocker | flag, the message
        d["decided_by"] = decided_by(self)
        return d


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
        self.heavy_stack: list[str] = []


def _score_title(profile: Profile, job: JobInput, a: JobAnalysis, ctx: _Ctx) -> float:
    prefs = profile.job_preferences
    title = _norm(job.title)
    variants = [job.title, normalize_title_aliases(job.title)]
    for kw in prefs.excluded_title_keywords:
        if any(keyword_in(kw, v) for v in variants):
            ctx.blockers.append(f"Title contains excluded keyword '{kw.strip()}'")
            return 0.0
    best_target = max((fuzz.token_set_ratio(title, _norm(t)) for t in prefs.target_titles), default=0)
    best_accept = max((fuzz.token_set_ratio(title, _norm(t)) for t in prefs.acceptable_titles), default=0)
    cat = a.title_category
    if cat == "fullstack":
        heavy, light = title_backend_stack(job.title)
        if heavy:
            ctx.heavy_stack = heavy
            ctx.reasons.append(f"Title '{job.title}' pairs the frontend with a {'/'.join(heavy)} backend "
                               "(backend-heavy stacks are deprioritised)")
            return 35.0 if prefs.accept_fullstack else 15.0
        if light and prefs.accept_fullstack:
            ctx.reasons.append(f"Title '{job.title}' is frontend + {'/'.join(light)} (accepted)")
            return 72.0
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


def share_evidence(a: JobAnalysis) -> str:
    """What the frontend share rests on, in words: the technologies the description itself names."""
    if not a.frontend_skills:
        return ("the description names no frontend technology"
                + (f" (it names {', '.join(a.backend_skills[:4])})" if a.backend_skills else ""))
    return (f"frontend: {', '.join(a.frontend_skills[:4])}; "
            f"backend/other: {', '.join(a.backend_skills[:4]) or 'none named'}")


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
    if a.title_category in {"generic", "other", "fullstack"} and ratio < min_ratio - 0.2:
        # far below your minimum, measured from the description: not a role to decide about, a role to leave out
        # ("Senior NONMEM Programmer", "Forward Deployed Engineer", "Full Stack Engineer" with a backend description)
        kind = "Backend-heavy full-stack role" if a.title_category == "fullstack" else "Not a frontend role"
        ctx.blockers.append(f"{kind}: frontend focus ≈ {pct}% (your minimum is {round(min_ratio * 100)}%) - "
                            f"{share_evidence(a)}")
    if a.title_category in _OTHER_FAMILIES:
        # "Java or Kotlin & React.js Developer", "Node Js Developer", "QA Engineer (React)": the title names another
        # kind of role and merely mentions a frontend technology. Its score alone never applies (61.5 against a
        # threshold of 60 had queued the first): below your minimum it is left out, above it the decision is yours.
        if ratio < min_ratio:
            ctx.blockers.append(f"Title names a {a.title_category} role and the frontend focus is ≈ {pct}% (your "
                                f"minimum is {round(min_ratio * 100)}%) - {share_evidence(a)}")
        else:
            ctx.flags.append(f"Title names a {a.title_category} role, although the description is mostly frontend "
                             f"work (≈ {pct}%)")
    if a.title_category == "fullstack" and ratio < min_ratio:
        ctx.flags.append(f"Full-stack role is backend-heavy (frontend ≈ {pct}%; {share_evidence(a)})")
    if a.title_category in {"generic", "other"} and ratio < min_ratio:
        ctx.flags.append(f"Generic title and frontend share is only ≈ {pct}% ({share_evidence(a)})")
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
    if a.experience_note and not a.experience_conflict:
        ctx.reasons.append(a.experience_note)
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
            if a.experience_conflict:  # "Senior ..." with "0-2 Yrs" in the portal's field: yours to judge, not a skip
                ctx.flags.append(a.experience_note)
                return 60.0
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
    if job == "junior" and SENIORITY_RANK[cand] >= SENIORITY_RANK["senior"]:
        # "Junior React Developer" scored 98.6 for a senior engineer (LinkedIn dry run, 2026-10-14): the title, the
        # description and the skills all fit - the level does not. Yours to decide, never applied to by the score.
        ctx.flags.append(f"Junior-level role (the title says so); your experience puts you at {cand} level")
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
    # Mandatory skills of another platform that you do not list: "Staff Frontend Engineer - Mobile" requires React
    # Native, Android and iOS; "React GenAI Developer" requires Java, Hibernate and Spring Boot. (Small tools a
    # description also lists as required - jQuery, Bootstrap, a test runner - are not what this is about.)
    foreign: dict[str, list[str]] = {}
    for skill in missing:
        d = skill_def(skill)
        if d is not None and d.category == "mobile":
            foreign.setdefault("mobile", []).append(skill)
        elif skill in _HEAVY_BACKEND_SKILLS:
            foreign.setdefault("backend", []).append(skill)
    for family, skills in foreign.items():
        if len(skills) >= 2:
            ctx.flags.append(f"The description requires {', '.join(skills[:4])} ({family}) - not among your skills")

    ratio = a.frontend_ratio
    mine = set(_profile_skill_map(profile))
    min_ratio = prefs.min_frontend_ratio
    if a.title_category in {"generic", "other"}:
        heavy_req = sorted(x for x in a.required_skills if x in _HEAVY_BACKEND_SKILLS)
        not_mine = [x for x in heavy_req if x not in mine]
        if not_mine and (ratio is None or ratio < min_ratio):
            # "Software Developer – Unity" (.NET), "Senior Software Engineer" (Java), "Drupal Developer" (PHP): a
            # generic title over a stack you do not list, and not frontend work - not a decision to put to you
            ctx.blockers.append(f"Not a frontend role: a generic title whose description centres on "
                                f"{', '.join(not_mine)} (not among your skills); frontend focus ≈ "
                                f"{round((ratio or 0) * 100)}% - {share_evidence(a)}")
        elif heavy_req and (ratio or 0) < 0.6:
            ctx.flags.append(f"Generic title; the description centres on {', '.join(heavy_req)}")
    if (a.title_category == "fullstack" and not ctx.heavy_stack and ratio is not None
            and min_ratio - 0.2 <= ratio < min_ratio):
        # React + Node.js / Python full-stack roles measure 35-50 % frontend: their backend is in your own stack
        # (listed in your skills, "React + Node.js Developer" among your acceptable titles) - not "backend-heavy"
        req = set(a.required_skills)
        if "Node.js" in mine:
            mine = mine | {"Express"}  # (Node.js's own web layer; heavier frameworks - NestJS - must be yours to count)
        foreign_req = sorted(s for s in req if s not in mine and (
            s in _PLATFORMS or ((d := skill_def(s)) is not None and d.category in {"data", "mobile"})))
        own = sorted(req & _PLATFORMS & mine)
        core = sorted(req & _FRONTEND_CORE & mine)
        foreign_platforms = [s for s in foreign_req if s in _PLATFORMS]
        if foreign_platforms and ratio < min_ratio - 0.1:
            # clearly backend-leaning (under 40 %) and built on a backend platform you do not list (.NET, Java, Django
            # ...): left out, like a generic title over the same stack. (40-50 %, nearly balanced: yours to decide.)
            ctx.flags = [f for f in ctx.flags if not f.startswith("Full-stack role is backend-heavy")]
            ctx.blockers.append(f"Backend-heavy full-stack role: frontend focus ≈ {round(ratio * 100)}% (your minimum "
                                f"is {round(min_ratio * 100)}%) and the backend requires {', '.join(foreign_platforms)} "
                                f"(not among your skills) - {share_evidence(a)}")
        elif core and own and not foreign_req and ratio >= min_ratio - 0.15:
            ctx.flags = [f for f in ctx.flags if not f.startswith("Full-stack role is backend-heavy")]
            ctx.reasons.append(f"Full-stack role whose backend is in your own stack ({', '.join(own)}) with "
                               f"{', '.join(core)} required: frontend ≈ {round(ratio * 100)}% - accepted")
        elif foreign_req:
            ctx.flags = [f"{f[:-1]}; not among your skills: {', '.join(foreign_req[:4])})"
                         if f.startswith("Full-stack role is backend-heavy") and f.endswith(")") else f for f in ctx.flags]
    _employment_and_kind(job, a, ctx)
    if ctx.heavy_stack:
        pct = round((ratio or 0) * 100)
        if ratio is None or ratio < 0.6:
            ctx.blockers.append(f"Backend-heavy full-stack role ({'/'.join(ctx.heavy_stack)} + frontend, frontend ≈ "
                                f"{pct}%)")
        else:
            ctx.flags.append(f"{'/'.join(ctx.heavy_stack)} full-stack title, but the description is frontend-heavy "
                             f"(≈ {pct}%)")

    score = round(sum(breakdown[k] * w for k, w in WEIGHTS.items()), 1)
    adjust, priority, ai_focus = _preference(job, a)
    if adjust:
        score = round(max(0.0, min(100.0, score + adjust)), 1)
        ctx.reasons.append(priority)
    breakdown["preference"] = adjust
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
    resume_category = "ai" if ai_focus else ("fullstack" if category in {"fullstack", "backend"}
                                             or (a.frontend_ratio is not None and a.frontend_ratio < 0.6) else "frontend")
    return MatchResult(
        score=score, decision=decision.value, rule_score=score,
        breakdown={k: round(v, 1) for k, v in breakdown.items()},
        frontend_ratio=None if a.frontend_ratio is None else round(a.frontend_ratio, 2),
        category=category, matched_skills=matched, missing_skills=missing,
        blockers=ctx.blockers, flags=ctx.flags, reasons=ctx.reasons, analysis=a.to_dict(),
        ai_focus=ai_focus, resume_category=resume_category, priority=priority,
    )


_OTHER_FAMILIES = {"backend", "mobile", "devops", "data", "qa"}  # title categories that are not yours by themselves

# backend languages / frameworks: "the backend is in your own stack" when every one a full-stack role requires is yours
_PLATFORMS = {".NET", "C++", "Django", "Go", "Hibernate", "Java", "PHP", "Ruby on Rails", "Rust", "Scala", "Spring Boot",
              "Python", "FastAPI", "Flask", "Node.js", "Express", "NestJS"}
_FRONTEND_CORE = {"React", "Next.js", "TypeScript", "JavaScript", "Angular"}
_MODERN_FRONTEND = {"React", "Next.js", "Angular", "Vue", "Svelte", "TypeScript", "Nuxt", "Remix", "Gatsby"}
_CMS = re.compile(r"\b(word\s?press|shopify|webflow|wix|drupal|joomla|magento|squarespace|elementor)\b", re.I)
# vendor platforms whose developer roles customise the platform (its scripting, its widgets) rather than build a web
# frontend - their descriptions name JavaScript, HTML, CSS and often AngularJS, and scored 86-88 as "frontend roles"
# ("ServiceNow Developer" x2, "Teamcenter AWC Developer" on 2026-10-15; the local model had put them to you)
_PLATFORM_ROLE = re.compile(r"\b(service\s?now|teamcenter|pega(?:systems)?|appian|outsystems|mendix|power\s?apps|"
                            r"dynamics\s?365|d365|sitecore|adobe\s+experience\s+manager|\baem\b|liferay|guidewire|"
                            r"workday|oracle\s+apex|siebel|temenos|sharepoint|opentext|documentum|maximo|adobe\s+msd|"
                            r"adobe\s+(?:campaign|target|commerce|analytics)|marketo|amazon\s+connect|"
                            r"oracle\s+integration\s+cloud|oic)\b", re.I)
# specialist titles your profile does not cover (a blockchain role read as 50 % frontend, game developers queued)
_SPECIALTY = re.compile(r"\b(blockchain|web3|solidity|smart\s+contracts?|game\s+(?:developer|engineer|programmer)|"
                        r"unity|unreal|roblox|firmware)\b", re.I)
# employment terms that are yours to decide (your profile does not say whether you take them)
_TERMS = [
    ("freelance", re.compile(r"\bfree[\s-]?lanc(?:e|er|ing)\b", re.I)),
    ("contract", re.compile(r"\bcontract(?:ual)?\s+(?:role|position|basis|job|opportunity|engagement|to\s+hire)\b|"
                            r"\b(?:employment|job|work)\s+type\s*[:\-]\s*(?:contract|temporary)\b", re.I)),
    ("part-time", re.compile(r"\bpart[\s-]time\b", re.I)),
    ("hourly pay", re.compile(r"(?:\$|usd|₹|inr)\s?\d[\d,]*(?:\.\d+)?\s*(?:/|per)\s*(?:hr|hour|task)\b", re.I)),
    ("walk-in interview", re.compile(r"\bwalk[\s-]?in\s+(?:drive|interview)s?\b|\bface\s+to\s+face\s+(?:interview|walk[\s-]?in)",
                                     re.I)),
    ("night shift / US hours", re.compile(r"\bnight\s+shift\b|\b(?:[6-9]|1[0-2])\s*(?::\d\d)?\s*(?:pm|p\.m\.)\s*(?:to|-|–)\s*"
                                          r"(?:[1-9]|1[0-2])\s*(?::\d\d)?\s*(?:am|a\.m\.)|\b(?:us|uk|pst|est|cst)\s+"
                                          r"(?:shift|hours|time\s*zone)\b", re.I)),
]


def _employment_and_kind(job: JobInput, a: JobAnalysis, ctx: _Ctx) -> None:
    """What the skill share cannot see: a CMS site-building role, a designer's role, terms that are yours to decide,
    and a share resting on almost no evidence (run 76: a WordPress / Shopify role and a "Freelance Web Designer &
    Developer" - two skill mentions, "responsive" and "accessibility" - were queued at 76.6 and 84.2)."""
    text = f"{job.title}\n{job.description or ''}"
    cms = sorted({m.lower().replace(" ", "") for m in _CMS.findall(text)})
    if cms and (_CMS.search(job.title or "") or len(_CMS.findall(text)) >= 3):
        names = ", ".join(n.title() for n in cms)
        if not set(a.skills) & _MODERN_FRONTEND and a.title_category != "frontend":
            ctx.blockers.append(f"CMS site-building role ({names}): the description names no React, Angular, Vue or "
                                "TypeScript work")
        elif not set(a.skills) & _MODERN_FRONTEND:  # a "Frontend Developer" title over CMS templates: yours to decide
            ctx.flags.append(f"CMS-centred role ({names}): the description names no React, Angular, Vue or TypeScript "
                             "work - check that it is the engineering work you want")  # (a frontend title: yours)
        else:
            ctx.flags.append(f"CMS-centred role ({names}) - check that it is the engineering work you want")
    platforms = sorted({re.sub(r"\s+", " ", m.lower()) for m in _PLATFORM_ROLE.findall(text)})
    if platforms and (_PLATFORM_ROLE.search(job.title or "") or len(_PLATFORM_ROLE.findall(text)) >= 3):
        names = ", ".join(n.title() for n in platforms)
        if not set(a.required_skills) & _MODERN_FRONTEND and a.title_category != "frontend":
            ctx.blockers.append(f"Platform-specific role ({names}): customising a vendor platform - the description "
                                "requires no React, Angular, Vue or TypeScript work")
        else:
            ctx.flags.append(f"Platform-specific role ({names}): customising a vendor platform rather than building a "
                             "web frontend - yours to decide")
    if m := _SPECIALTY.search(job.title or ""):
        ctx.flags.append(f"A specialist role ({m.group(1).title()}) - your profile does not list it; yours to decide")
    if re.search(r"\bdesigner\b", job.title or "", re.I):
        ctx.flags.append(f"Title names a design role ('{job.title}') - not a developer role by itself")
    terms = [name for name, rx in _TERMS if rx.search(text)]
    if terms:
        ctx.flags.append(f"Employment terms are yours to decide: {', '.join(terms)}")
    distinct = [s for s in a.skills if (d := skill_def(s)) is not None and not d.signal_only]
    if a.title_category != "frontend" and 0 < len(distinct) < 3:
        ctx.flags.append(f"Few recognizable skills in the job description (only {', '.join(distinct)})")

_HEAVY_BACKEND_SKILLS = {"Java", "Spring Boot", "Hibernate", ".NET", "PHP", "Go", "Ruby on Rails", "Scala", "C++"}

_REACT_TITLE = re.compile(r"\breact(?:\.?js)?\b(?![\s-]*native)|next\.?js", re.I)
_TYPESCRIPT_TITLE = re.compile(r"\btypescript\b", re.I)
_ANGULAR_TITLE = re.compile(r"\bangular(?:js)?\b", re.I)


def _preference(job: JobInput, a: JobAnalysis) -> tuple[float, str, bool]:
    """Your stated priorities on top of the fit score: React / Next.js first, frontend + AI welcome,
    Angular-only roles a little lower. Returns (score adjustment, note, frontend+AI flag)."""
    ai_mentions = sum(n for s, n in a.skills.items() if (d := skill_def(s)) and d.category == "ai")
    frontendish = a.title_category == "frontend" or (a.frontend_ratio or 0) >= 0.5
    ai_in_title = bool(re.search(r"\bai\b|\bgen\s?ai\b|\bllm\b|\bml\b", job.title or "", re.I))
    ai_focus = frontendish and (ai_mentions >= 3 or (ai_in_title and ai_mentions >= 1))
    adjust, notes = 0.0, []
    if _REACT_TITLE.search(job.title or ""):
        adjust += 3
        notes.append("React/Next.js role (top priority)")
    elif _TYPESCRIPT_TITLE.search(job.title or "") and not _ANGULAR_TITLE.search(job.title or ""):
        adjust += 3
        notes.append("TypeScript role (top priority)")
    elif _ANGULAR_TITLE.search(job.title or ""):
        adjust -= 5
        notes.append("Angular-focused role (React/Next.js preferred)")
    if ai_focus:
        adjust += 3
        notes.append("Frontend + AI role")
    return adjust, "Priority: " + ", ".join(notes) if notes else "", ai_focus


def preferences_rev(profile: Profile) -> str:
    """A short fingerprint of everything in your profile that decides whether a job is relevant. It is stored with
    every decision, so a job skipped under older preferences can be told apart and looked at again."""
    import hashlib
    import json

    wp = profile.work_preferences
    data = {
        "job": profile.job_preferences.model_dump(mode="json"),
        "matching": profile.matching.model_dump(mode="json"),
        "work": {"locations": sorted(p.strip().lower() for p in wp.preferred_locations), "remote": wp.open_to_remote,
                 "hybrid": wp.open_to_hybrid, "onsite": wp.open_to_onsite, "relocate": wp.willing_to_relocate},
        "years": profile.professional.total_experience_years,
        "skills": [sorted(s.lower() for s in profile.skills.primary), sorted(s.lower() for s in profile.skills.secondary)],
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:12]


def skip_reason(result: MatchResult, apply_threshold: float) -> str:
    """Why a job with hard blockers is skipped, in words that also explain a high score next to it: the score
    measures the overall fit, a hard requirement overrides it."""
    why = "; ".join(result.blockers)
    if result.score >= apply_threshold:
        why += f" (a hard requirement - skipped although the overall fit is {result.score:g})"
    return why


def decision_reason(result: MatchResult, apply_threshold: float) -> str:
    """The sentence recorded with a decision: the hard requirement, the low score with its main reasons, or what
    makes the job yours to decide. Never empty for a skip."""
    if result.blockers:
        return skip_reason(result, apply_threshold)
    if result.decision == Decision.SKIP.value:
        return f"Low match ({result.score}): " + ("; ".join(result.reasons[:2]) or "little of the role fits your profile")
    return "; ".join(result.flags) or (result.reasons[0] if result.reasons else "")


def title_prescreen(profile: Profile, title: str) -> list[str]:
    """Hard blockers visible from the title alone (excluded keywords, intern/director level, clearly
    non-frontend role families). Lets discovery skip a job without opening its page."""
    from app.matching.analyzer import classify_title, title_seniority

    reasons: list[str] = []
    variants = [title, normalize_title_aliases(title)]
    for kw in profile.job_preferences.excluded_title_keywords:
        if any(keyword_in(kw, v) for v in variants):
            reasons.append(f"Title contains excluded keyword '{kw.strip()}'")
            return reasons
    level = title_seniority(title)
    if level == "intern":
        reasons.append("Internship/trainee role")
    elif level == "director":
        reasons.append("Director/VP/head-level role")
    cat = classify_title(title)
    if cat in {"mobile", "devops", "data", "qa"} and not re.search(
            r"front[\s-]?end|\breact\b(?![\s-]*native)|\bangular\b|\bui\b|\bweb\b|javascript|typescript", title, re.I):
        reasons.append(f"Title looks like a {cat} role")
    return reasons
