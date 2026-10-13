"""Verified candidate knowledge: every fact the assistant may use, with the profile key it came from.

Generated answers may only combine and rephrase these facts (plus the job description). Derivations
(e.g. years with a skill from the work-history dates) are computed here, never estimated by a model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from app.matching.analyzer import JobInput
from app.matching.skills import canonicalize, extract_skills, skill_def
from app.profile import Profile, WorkExperience

_OUTCOME = re.compile(r"\b(?:improv|reduc|increas|cut|grew|grow|sav|launch|deliver|led|lead|won|award|migrat|"
                      r"optimi[sz]ed|scaled|built\s+and\s+(?:launched|shipped)|from\s+\d|by\s+\d+\s*%|\d+\s*%|\d+x\b)", re.I)

# Recruiter-friendly themes recognised in your summary / highlights / role summaries.
THEMES: list[tuple[str, re.Pattern[str]]] = [
    ("frontend performance and Core Web Vitals", re.compile(r"core\s+web\s+vitals|web\s+performance|performance\s+optimi", re.I)),
    ("accessibility", re.compile(r"accessib|a11y|wcag", re.I)),
    ("scalable enterprise applications", re.compile(r"enterprise|scalab", re.I)),
    ("micro frontends", re.compile(r"micro[\s-]?front", re.I)),
    ("frontend testing", re.compile(r"\btesting\b|jest|playwright|cypress|testing\s+library", re.I)),
    ("AI-powered features (LLMs, RAG and AI agents)", re.compile(r"\bai[\s-]powered|\bllms?\b|\brag\b|ai\s+agents?|openai", re.I)),
    ("GraphQL and REST API integration", re.compile(r"graphql|rest\s+apis?", re.I)),
    ("SEO-friendly, high-traffic web applications", re.compile(r"\bseo\b|high[\s-]traffic", re.I)),
    ("design systems and component libraries", re.compile(r"design\s+system|component\s+librar", re.I)),
]


# skills that read better as themes ("frontend performance") than in a technology list
THEME_SKILLS = {"Web Performance", "Accessibility", "Responsive Design", "Micro Frontends", "Design Systems",
                "REST APIs", "Git", "CI/CD", "Agile"}


@dataclass
class Fact:
    key: str
    text: str
    kind: str  # summary|highlight|role|skills|education|project|preference
    skills: set[str] = field(default_factory=set)


def _parse_ym(value: str, today: date) -> date | None:
    v = (value or "").strip().lower()
    if not v or v in {"present", "current", "now", "till date", "today"}:
        return today if v else None
    m = re.match(r"(\d{4})(?:-(\d{1,2}))?", v)
    if not m:
        return None
    return date(int(m.group(1)), int(m.group(2) or 1), 1)


def role_interval(w: WorkExperience, today: date | None = None) -> tuple[date, date] | None:
    today = today or date.today()
    start = _parse_ym(w.start, today)
    end = _parse_ym(w.end or "present", today)
    if start is None or end is None or end < start:
        return None
    return start, end


def _merge_years(intervals: list[tuple[date, date]]) -> float:
    if not intervals:
        return 0.0
    intervals = sorted(intervals)
    total, (cur_s, cur_e) = 0, intervals[0]
    for s, e in intervals[1:]:
        if s <= cur_e:
            cur_e = max(cur_e, e)
        else:
            total += (cur_e - cur_s).days
            cur_s, cur_e = s, e
    total += (cur_e - cur_s).days
    return total / 365.25


def floor_half(years: float) -> float:
    """6.58 -> 6.5: never round experience up."""
    return int(years * 2) / 2


def fmt_years(years: float) -> str:
    v = floor_half(years)
    return str(int(v)) if float(v).is_integer() else f"{v:.1f}"


def first_person(summary: str, current: bool = True) -> str:
    """'Developed and maintained ...' -> 'developed and maintained ...'; 'Leads the ...' -> 'lead the ...'.

    Converts the first sentence only (what follows "I"); see first_person_sentences for whole summaries."""
    s = summary.strip().rstrip(".")
    if not s:
        return ""
    words = s.split(" ", 1)
    head, rest = words[0], (words[1] if len(words) > 1 else "")
    low = head.lower()
    if low == "working":
        return f"{'am' if current else 'was'} working {rest}".strip()
    if low == "involved":
        return f"{'am' if current else 'was'} also involved {rest}".strip()
    if low in {"responsible", "focused"}:
        return f"{'am' if current else 'was'} {low} {rest}".strip()
    if re.fullmatch(r"(?:lead|own|build|manage|develop|design|maintain|drive|work|create|deliver|implement)s", low):
        return f"{low[:-1]} {rest}".strip()
    if low.endswith("ed") or low in {"built", "led", "ran", "wrote", "made", "drove", "built,"}:
        return f"{low} {rest}".strip()
    return f"{'work' if current else 'worked'} on {s[0].lower() + s[1:]}"


def first_person_sentences(summary: str, current: bool = True) -> list[str]:
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", summary or "") if p.strip()]
    return [f"I {first_person(p, current)}." for p in parts]


class CandidateKnowledge:
    def __init__(self, profile: Profile, today: date | None = None) -> None:
        self.profile = profile
        self.today = today or date.today()
        self.facts: list[Fact] = []
        p, pr = profile.personal, profile.professional
        if pr.summary and "YOUR_" not in pr.summary:
            self._add("professional.summary", pr.summary, "summary")
        for i, h in enumerate(pr.highlights):
            if h.strip():
                self._add(f"professional.highlights[{i}]", h, "highlight")
        for i, w in enumerate(pr.work_history):
            if not (w.company or w.title):
                continue
            span = f"{w.start or '?'} to {w.end or 'present'}"
            text = f"{w.title} at {w.company} ({span})" + (f": {w.summary}" if w.summary else "")
            if w.technologies:
                text += f" Technologies: {', '.join(w.technologies)}."
            self._add(f"professional.work_history[{i}]", text, "role",
                      {canonicalize(t) for t in w.technologies} | set(extract_skills(w.summary)))
        for i, pj in enumerate(getattr(profile, "projects", []) or []):
            text = " ".join(x for x in [pj.name + ":" if pj.name else "", pj.description, pj.outcome,
                                        ("Technologies: " + ", ".join(pj.technologies)) if pj.technologies else ""] if x)
            if text.strip():
                self._add(f"projects[{i}]", text, "project", {canonicalize(t) for t in pj.technologies})
        if profile.skills.primary:
            self._add("skills.primary", ", ".join(profile.skills.primary), "skills")
        if profile.skills.secondary:
            self._add("skills.secondary", ", ".join(profile.skills.secondary), "skills")
        for i, e in enumerate(pr.education):
            if e.degree:
                self._add(f"professional.education[{i}]",
                          f"{e.degree}{' in ' + e.field if e.field else ''}{', ' + e.institution if e.institution else ''}"
                          f"{' (' + str(e.end_year) + ')' if e.end_year else ''}", "education")
        if pr.certifications:
            self._add("professional.certifications", ", ".join(pr.certifications), "education")
        self.name = p.full_name

    def _add(self, key: str, text: str, kind: str, skills: set[str] | None = None) -> None:
        self.facts.append(Fact(key, re.sub(r"\s+", " ", text).strip(), kind,
                               skills if skills is not None else {canonicalize(s) for s in extract_skills(text)}))

    # ------------------------------------------------------------ skills
    @property
    def skill_set(self) -> set[str]:
        return {canonicalize(s) for s in self.profile.skills.all}

    def has_skill(self, skill: str) -> bool:
        c = canonicalize(skill)
        return c in self.skill_set or any(c in f.skills for f in self.facts if f.kind in {"role", "project"})

    def roles_using(self, skill: str) -> list[tuple[int, WorkExperience]]:
        c = canonicalize(skill)
        out = []
        for i, w in enumerate(self.profile.professional.work_history):
            techs = {canonicalize(t) for t in w.technologies} | {canonicalize(s) for s in extract_skills(w.summary)}
            if c in techs:
                out.append((i, w))
        return out

    def skill_years(self, skill: str) -> tuple[float | None, list[str]]:
        """Explicit skills.years, else the merged duration of the roles that list the skill."""
        c = canonicalize(skill)
        explicit = self.profile.skills.years_for(c)
        if explicit is None:
            explicit = next((v for k, v in self.profile.skills.years.items() if canonicalize(k) == c and v is not None),
                            None)
        if explicit is not None:
            return float(explicit), [f"skills.years['{c}']"]
        roles = self.roles_using(c)
        intervals = [iv for _, w in roles if (iv := role_interval(w, self.today))]
        if not intervals:
            return None, []
        return _merge_years(intervals), [f"professional.work_history[{i}]" for i, _ in roles]

    def frontend_years(self) -> tuple[float | None, list[str]]:
        pr = self.profile.professional
        if pr.relevant_experience_years:
            return float(pr.relevant_experience_years), ["professional.relevant_experience_years"]
        roles = [(i, w) for i, w in enumerate(pr.work_history)
                 if re.search(r"front[\s-]?end|\bui\b|react|angular|web", f"{w.title} {w.summary}", re.I)]
        intervals = [iv for _, w in roles if (iv := role_interval(w, self.today))]
        if not intervals:
            return None, []
        return _merge_years(intervals), [f"professional.work_history[{i}]" for i, _ in roles]

    def total_years_phrase(self) -> tuple[str, list[str]]:
        yrs = self.profile.professional.total_experience_years
        if yrs is None:
            return "", []
        n = fmt_years(yrs)
        plus = any(f"{n}+" in f.text for f in self.facts if f.kind in {"summary", "highlight"})
        return f"{n}{'+' if plus else ''} years", ["professional.total_experience_years"]

    # ------------------------------------------------------------ roles
    def current_role(self) -> tuple[int, WorkExperience] | None:
        for i, w in enumerate(self.profile.professional.work_history):
            if (w.end or "present").strip().lower() in {"present", "current", "now", "till date", ""} and w.company:
                return i, w
        return None

    def recent_roles(self) -> list[tuple[int, WorkExperience]]:
        roles = [(i, w) for i, w in enumerate(self.profile.professional.work_history) if w.company]
        return sorted(roles, key=lambda iw: (role_interval(iw[1], self.today) or (date.min, date.min))[1], reverse=True)

    # ------------------------------------------------------------ relevance to the job
    def matching_skills(self, job: JobInput | None, limit: int = 4) -> list[str]:
        """Your skills the job asks for (most emphasised first); your primary skills when the JD says little."""
        mine_primary = [canonicalize(s) for s in self.profile.skills.primary]
        mine = list(dict.fromkeys(mine_primary + [canonicalize(s) for s in self.profile.skills.secondary]))
        out: list[str] = []
        if job is not None:
            jd = extract_skills(f"{job.title}\n{job.title}\n{job.description}\n{' '.join(job.skills)}")
            ranked = sorted(jd.items(), key=lambda kv: (-kv[1], mine.index(kv[0]) if kv[0] in mine else 99))
            out = [s for s, _ in ranked if s in mine and (d := skill_def(s)) and not d.signal_only
                   and s not in THEME_SKILLS]
        for s in mine_primary:
            if len(out) >= limit:
                break
            if s not in out and s not in THEME_SKILLS and (d := skill_def(s)) and d.category == "frontend" \
                    and not d.signal_only:
                out.append(s)
        return out[:limit]

    def themes(self, job: JobInput | None, limit: int = 3, jd_only: bool = False) -> list[tuple[str, list[str]]]:
        """Themes present in your verified text, those the job also mentions first."""
        jd = f"{job.title} {job.description}" if job else ""
        found: list[tuple[int, str, list[str]]] = []
        for phrase, rx in THEMES:
            keys = [f.key for f in self.facts if f.kind in {"summary", "highlight", "role", "project"} and rx.search(f.text)]
            in_jd = bool(jd and rx.search(jd))
            if keys and (in_jd or not jd_only):
                found.append((0 if in_jd else 1, phrase, keys[:2]))
        found.sort(key=lambda t: t[0])
        return [(phrase, keys) for _, phrase, keys in found[:limit]]

    def relevant_facts(self, job: JobInput | None, kinds: set[str] | None = None, limit: int = 4) -> list[Fact]:
        jd_skills = set(extract_skills(f"{job.title} {job.description}")) if job else set()
        cands = [f for f in self.facts if kinds is None or f.kind in kinds]
        return sorted(cands, key=lambda f: -len(f.skills & jd_skills))[:limit]

    def achievements(self) -> list[Fact]:
        return [f for f in self.facts if f.kind in {"highlight", "project"} and _OUTCOME.search(f.text)
                and not re.match(r"^\s*(?:\d+\+?\s+years?|strong|experience|experienced)\b", f.text, re.I)]

    def project_facts(self) -> list[Fact]:
        projects = [f for f in self.facts if f.kind == "project"]
        if projects:
            return projects
        # concrete role summaries (what was built, with which technologies) can describe project work
        out = []
        for i, w in enumerate(self.profile.professional.work_history):
            if len((w.summary or "").split()) >= 12 and (w.technologies or extract_skills(w.summary)):
                out.extend(f for f in self.facts if f.key == f"professional.work_history[{i}]")
        return out

    # ------------------------------------------------------------ for prompts / validation
    def prompt_block(self, job: JobInput | None, limit_chars: int = 3500) -> str:
        lines = [f"[{f.key}] {f.text}" for f in self.facts]
        total, _ = self.total_years_phrase()
        if total:
            lines.insert(0, f"[professional.total_experience_years] {total} of professional experience")
        if self.profile.professional.current_title:
            lines.insert(1, f"[professional.current_title] {self.profile.professional.current_title}"
                            + (f" at {self.profile.professional.current_company}" if self.profile.professional.current_company
                               else ""))
        text = "\n".join(lines)
        return text[:limit_chars]

    def keys(self) -> set[str]:
        return {f.key for f in self.facts} | {"professional.total_experience_years", "professional.current_title",
                                             "professional.current_company", "job.description", "job.title",
                                             "job.company", "page"}

    def corpus(self) -> str:
        return "\n".join(f.text for f in self.facts) + "\n" + self.name
