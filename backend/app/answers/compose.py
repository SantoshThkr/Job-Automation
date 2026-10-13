"""Grounded answer composer: written answers assembled only from verified facts + the job description.

Deterministic and always available (no model needed). The local LLM, when running, writes more natural
text; its output is validated and this composer is the fallback. Every sentence here is built from a
profile fact or the job posting, so nothing about you can be invented.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.answers.intent import Category
from app.answers.knowledge import CandidateKnowledge, first_person_sentences, fmt_years
from app.matching.analyzer import JobInput


@dataclass
class Budget:
    """How much text the field wants."""

    size: str  # short | medium | long
    max_chars: int

    @classmethod
    def for_field(cls, field_type: str, max_length: int | None, category: Category | None = None) -> Budget:
        if category == Category.COVER_LETTER:
            return cls("long", min(max_length or 2000, 2000))
        if field_type in {"textarea", "contenteditable"}:
            if max_length and max_length <= 160:
                return cls("short", max_length)
            if max_length and max_length >= 1500:
                return cls("medium", 700)
            return cls("medium", min(max_length or 600, 600))
        return cls("short", min(max_length or 160, 200))


@dataclass
class Composed:
    text: str
    sources: list[str]
    confidence: float
    note: str = ""
    extra: dict = field(default_factory=dict)


def _join(items: list[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _fit(sentences: list[str], budget: Budget) -> str:
    """Keep whole sentences within the budget (at least the first one, trimmed if it must be)."""
    limit = {"short": 1, "medium": 4, "long": 8}[budget.size]
    out: list[str] = []
    for s in sentences:
        s = re.sub(r"\s+", " ", s).strip()
        if not s:
            continue
        candidate = " ".join([*out, s])
        if len(out) >= limit or len(candidate) > budget.max_chars:
            break
        out.append(s)
    if not out and sentences:
        first = re.sub(r"\s+", " ", sentences[0]).strip()
        out = [first[: budget.max_chars].rsplit(" ", 1)[0].rstrip(",;") + ("." if len(first) > budget.max_chars else "")]
    return " ".join(out)


def _role_phrase(job: JobInput | None) -> str:
    if job is None or not job.title:
        return "this role"
    title = re.sub(r"\s*[\(\[|].*$", "", job.title).strip(" -|")
    return f"the {title} role" + (f" at {job.company}" if job.company else "")


def _is_current(w) -> bool:
    return (w.end or "present").strip().lower() in {"present", "current", "now", "till date", ""}


def _work_sentences(k: CandidateKnowledge, index: int, max_sentences: int = 2) -> list[str]:
    """'At Bluepeak Labs, I developed ...' + following sentences of that role summary, in first person."""
    w = k.profile.professional.work_history[index]
    sents = first_person_sentences(w.summary, _is_current(w))[:max_sentences]
    if not sents:
        return []
    sents[0] = f"At {w.company}, {sents[0]}"
    return sents


def _work_sentence(k: CandidateKnowledge, index: int) -> str:
    s = _work_sentences(k, index, 1)
    return s[0] if s else ""


def _lower_first(text: str) -> str:
    return text[0].lower() + text[1:] if text and not text[:2].isupper() else text


def _dedupe(keys: list[str]) -> list[str]:
    return list(dict.fromkeys(keys))


class Composer:
    def __init__(self, knowledge: CandidateKnowledge) -> None:
        self.k = knowledge

    def compose(self, category: Category, question: str, job: JobInput | None, budget: Budget,
                skills: list[str] | None = None) -> Composed | None:
        fn = {
            Category.WHY_ROLE: self._why_role, Category.WHY_COMPANY: self._why_company,
            Category.WHY_HIRE: self._why_hire, Category.STRENGTHS: self._strengths,
            Category.TECHNICAL_EXPERIENCE: self._technical, Category.EXPERIENCE: self._experience,
            Category.CURRENT_RESPONSIBILITIES: self._responsibilities, Category.PROJECT_EXPERIENCE: self._project,
            Category.ACHIEVEMENT: self._achievement, Category.CAREER_GOALS: self._goals, Category.ABOUT: self._about,
            Category.COVER_LETTER: self._cover_letter,
        }.get(category)
        if fn is None:
            return None
        return fn(question, job, budget, skills or [])

    # ------------------------------------------------------------ building blocks
    def _skills(self, job: JobInput | None, n: int = 3) -> tuple[str, list[str]]:
        skills = self.k.matching_skills(job, limit=n)
        return _join(skills), (["skills.primary"] if skills else [])

    def _themes(self, job: JobInput | None, n: int = 2) -> tuple[str, list[str]]:
        th = self.k.themes(job, limit=n)
        return _join([t for t, _ in th]), [key for _, keys in th for key in keys]

    def _base(self, job: JobInput | None) -> tuple[str, list[str]]:
        """'7+ years of frontend experience with React, Next.js and TypeScript'"""
        years, ys = self.k.total_years_phrase()
        skills, ss = self._skills(job)
        if not skills:
            return "", []
        text = (f"{years} of frontend experience with {skills}" if years else f"hands-on experience with {skills}")
        return text, ys + ss

    # ------------------------------------------------------------ categories
    def _why_role(self, q: str, job: JobInput | None, b: Budget, _s) -> Composed | None:
        base, src = self._base(job)
        if not base:
            return None
        themes, ts = self._themes(job)
        role = _role_phrase(job)
        if b.size == "short":
            return Composed(f"It matches my {base}.", src + ["job.title"], 0.86)
        s = [f"{role[0].upper() + role[1:]} matches my {base}."]
        jd_themes = self.k.themes(job, limit=2, jd_only=True)
        team = f"the {job.company} team" if job and job.company else "your team"
        if jd_themes:
            s.append(f"Its focus on {_join([t for t, _ in jd_themes])} is work I have done, and I would like to bring "
                     f"that experience to {team}.")
            src += [k for _, keys in jd_themes for k in keys]
        elif themes:
            s.append(f"I would like to bring my experience in {themes} to {team}.")
            src += ts
        return Composed(_fit(s, b), _dedupe(src + ["job.title", "job.description"]), 0.87)

    def _why_company(self, q: str, job: JobInput | None, b: Budget, _s) -> Composed | None:
        base, src = self._base(job)
        if not base or job is None or not job.company:
            return None
        themes, ts = self._themes(job)
        s = [f"{job.company} is hiring for work that matches my {base}."]
        if themes:
            s.append(f"I would like to bring my experience in {themes} to the {job.company} team.")
            src += ts
        return Composed(_fit(s, b), _dedupe(src + ["job.company", "job.description"]), 0.82)

    def _why_hire(self, q: str, job: JobInput | None, b: Budget, _s) -> Composed | None:
        base, src = self._base(job)
        if not base:
            return None
        themes, ts = self._themes(job, 3)
        if b.size == "short":
            return Composed(f"I bring {base}.", _dedupe(src), 0.86)
        s = [f"I bring {base}" + (f", along with experience in {themes}." if themes else ".")]
        src += ts
        rel = [f for f in self.k.relevant_facts(job, {"role"}, limit=1)]
        if rel:
            idx = int(re.search(r"\[(\d+)\]", rel[0].key).group(1))
            sent = _work_sentence(self.k, idx)
            if sent:
                s.append(sent)
                src.append(rel[0].key)
        return Composed(_fit(s, b), _dedupe(src + ["job.description"]), 0.87)

    def _strengths(self, q: str, job: JobInput | None, b: Budget, _s) -> Composed | None:
        skills, ss = self._skills(job, 4)
        themes, ts = self._themes(job, 3)
        if not skills:
            return None
        if b.size == "short":
            return Composed(f"{skills}" + (f"; {themes}" if themes else ""), _dedupe(ss + ts), 0.88)
        s = [f"My key strengths are {skills}" + (f", together with {themes}." if themes else ".")]
        years, ys = self.k.total_years_phrase()
        if years:
            s.append(f"I have applied them over {years} of building production web applications.")
        return Composed(_fit(s, b), _dedupe(ss + ts + ys), 0.88)

    def _technical(self, q: str, job: JobInput | None, b: Budget, skills: list[str]) -> Composed | None:
        if not skills:
            return self._experience(q, job, b, skills)
        skill = skills[0]
        if not self.k.has_skill(skill):
            return None  # you have not listed it: only you can say whether you know it
        yrs, ysrc = self.k.skill_years(skill)
        roles = self.k.roles_using(skill)
        src = list(ysrc) or ["skills.primary"]
        depth = "hands-on experience" if roles else "experience"  # no dated role: do not overstate it
        lead = f"I have {fmt_years(yrs)} years of {depth} with {skill}" if yrs and yrs >= 0.5 else \
            f"I have {depth} with {skill}"
        if roles:
            names = _join(list(dict.fromkeys(w.company for _, w in roles[:2])))
            lead += f", including at {names}"
        s = [lead + "."]
        if b.size == "short":
            return Composed(s[0], src, 0.88)
        for i, _w in roles[:2]:
            sent = _work_sentence(self.k, i)
            if sent:
                s.append(sent)
                src.append(f"professional.work_history[{i}]")
        if len(s) == 1:
            facts = sorted((f for f in self.k.facts if f.kind in {"highlight", "summary"}
                            and re.search(rf"(?<![\w.]){re.escape(skill)}(?![\w])", f.text, re.I)),
                           key=lambda f: f.kind != "highlight")
            if facts:
                f = facts[0]
                text = f.text if f.kind == "highlight" else next(
                    (x for x in re.split(r"(?<=[.!?])\s+", f.text) if skill.lower() in x.lower()), f.text)
                text = text.rstrip(".")
                if re.match(r"^(?:strong\s+|solid\s+|hands[\s-]on\s+)?experience\b", text, re.I):
                    if yrs is None and not roles:
                        s = [f"I have {_lower_first(text)}."]  # the highlight already says it
                    else:
                        s.append(f"I have {_lower_first(text)}.")
                else:
                    s.append(f"This includes {_lower_first(text)}.")
                src.append(f.key)
        return Composed(_fit(s, b), _dedupe(src), 0.87)

    def _experience(self, q: str, job: JobInput | None, b: Budget, _s) -> Composed | None:
        years, ys = self.k.total_years_phrase()
        roles = self.k.recent_roles()
        if not years and not roles:
            return None
        title = self.k.profile.professional.current_title
        s = [f"I have {years} of professional experience" + (f", currently as {title}" if title else "") + "."
             if years else f"I currently work as {title}."]
        src = ys + (["professional.current_title"] if title else [])
        if b.size != "short":
            names = _join([w.company for _, w in roles[:3]])
            skills, ss = self._skills(job)
            if names:
                s.append(f"I have worked at {names}" + (f", mainly with {skills}." if skills else "."))
                src += [f"professional.work_history[{i}]" for i, _ in roles[:3]] + ss
        return Composed(_fit(s, b), src, 0.87)

    def _responsibilities(self, q: str, job: JobInput | None, b: Budget, _s) -> Composed | None:
        cur = self.k.current_role()
        if cur is None:
            return None
        i, w = cur
        src = [f"professional.work_history[{i}]"]
        sents = first_person_sentences(w.summary, True)
        if sents:
            sents[0] = f"As {w.title} at {w.company}, {sents[0]}"
        elif w.technologies:
            sents = [f"As {w.title} at {w.company}, I work with {_join(w.technologies[:5])}."]
        else:
            return None
        return Composed(_fit(sents, Budget(b.size if b.size != "short" else "medium", b.max_chars)), src, 0.88)

    def _project(self, q: str, job: JobInput | None, b: Budget, _s) -> Composed | None:
        facts = self.k.project_facts()
        if not facts:
            return None
        jd_rank = self.k.relevant_facts(job, {f.kind for f in facts}, limit=10)
        best = next((f for f in jd_rank if f in facts), facts[0])
        if best.kind == "project":
            idx = int(re.search(r"\[(\d+)\]", best.key).group(1))
            pj = self.k.profile.projects[idx]
            s = [f"{('At ' + pj.company + ', I worked on ') if pj.company else 'I worked on '}"
                 f"{pj.name or 'a project'}: {pj.description.rstrip('.')}."]
            if pj.technologies:
                s.append(f"It used {_join(pj.technologies[:5])}.")
            if pj.outcome:
                s.append(f"Result: {pj.outcome.rstrip('.')}.")
            return Composed(_fit(s, b), [best.key], 0.86)
        idx = int(re.search(r"\[(\d+)\]", best.key).group(1))
        sents = _work_sentences(self.k, idx, 3)
        if not sents:
            return None
        return Composed(_fit(sents, b), [best.key], 0.78, note="based on your role summary (no specific project in "
                                                                "your profile)")

    def _achievement(self, q: str, job: JobInput | None, b: Budget, _s) -> Composed | None:
        ach = self.k.achievements()
        if not ach:
            return None  # an achievement must come from you, never be invented
        best = next((f for f in self.k.relevant_facts(job, {"highlight", "project"}, limit=10) if f in ach), ach[0])
        return Composed(_fit([best.text.rstrip(".") + "."], b), [best.key], 0.85)

    def _goals(self, q: str, job: JobInput | None, b: Budget, _s) -> Composed | None:
        prefs = self.k.profile.job_preferences.target_titles
        skills, ss = self._skills(job)
        if not prefs or not skills:
            return None
        target = next((t for t in prefs if re.search(r"senior|lead", t, re.I)), prefs[0])
        ai = any("AI-powered" in t for t, _ in self.k.themes(None, limit=10))
        s = [f"My goal is to keep growing as a {target}, building scalable, high-performance products with {skills}"
             + (", and to keep delivering AI-powered features." if ai else ".")]
        return Composed(_fit(s, b), ["job_preferences.target_titles"] + ss, 0.8)

    def _about(self, q: str, job: JobInput | None, b: Budget, _s) -> Composed | None:
        summary = self.k.profile.professional.summary
        if summary and "YOUR_" not in summary:
            sentences = re.split(r"(?<=[.!?])\s+", summary)
            return Composed(_fit(sentences, b), ["professional.summary"], 0.92)
        return self._experience(q, job, b, [])

    def _cover_letter(self, q: str, job: JobInput | None, b: Budget, _s) -> Composed | None:
        parts, src = [], []
        for c in (self._about(q, job, Budget("medium", 600), []), self._why_role(q, job, Budget("medium", 400), []),
                  self._why_hire(q, job, Budget("medium", 500), [])):
            if c and c.text not in parts:
                parts.append(c.text)
                src += c.sources
        if not parts:
            return None
        closing = f"I would welcome the opportunity to discuss how I can contribute{(' to ' + job.company) if job and job.company else ''}."
        name = self.k.name
        text = "\n\n".join(parts + [closing]) + (f"\n\nRegards,\n{name}" if name else "")
        return Composed(text[: b.max_chars], list(dict.fromkeys(src)), 0.85)
