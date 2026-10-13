"""Candidate profile: schema, loading (with hot reload), validation and persistence.

The profile (config/profile.json) is the single source of truth for candidate facts.
The assistant never invents facts that are not present here.
"""

from __future__ import annotations

import json
from datetime import date
import logging
import shutil
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import PROJECT_ROOT, get_settings

log = logging.getLogger(__name__)

PLACEHOLDER_MARKERS = ("YOUR_", "XXXX", "example.com", "<", "CHANGE_ME")


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Personal(_Model):
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    phone_country_code: str = "+91"
    current_city: str = ""
    state: str = ""
    country: str = "India"
    postal_code: str = ""
    address: str = ""
    linkedin_url: str = ""
    github_url: str = ""
    portfolio_url: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def phone_national(self) -> str:
        digits = "".join(c for c in self.phone if c.isdigit())
        cc = "".join(c for c in self.phone_country_code if c.isdigit())
        if cc and digits.startswith(cc) and len(digits) > 10:
            return digits[len(cc):]
        return digits


class Education(_Model):
    degree: str = ""
    field: str = ""
    institution: str = ""
    start_year: int | None = None
    end_year: int | None = None
    grade: str = ""


class WorkExperience(_Model):
    company: str = ""
    title: str = ""
    start: str = ""  # YYYY-MM
    end: str = ""  # YYYY-MM or "present"
    location: str = ""
    summary: str = ""
    technologies: list[str] = Field(default_factory=list)


class Professional(_Model):
    current_title: str = ""
    current_company: str = ""
    total_experience_years: float | None = None
    relevant_experience_years: float | None = None
    notice_period_days: int | None = None
    serving_notice: bool = False
    last_working_day: str = ""
    current_ctc_lpa: float | None = None
    expected_ctc_lpa: float | None = None
    salary_negotiable: bool = True
    summary: str = ""
    highlights: list[str] = Field(default_factory=list)
    work_history: list[WorkExperience] = Field(default_factory=list)
    # true = the list above names every employer you have worked for, so "Have you worked for us before?" is
    # answered "No" for any other company. Unset = that question is always left for you.
    work_history_complete: bool | None = None
    education: list[Education] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=lambda: ["English"])


class Project(_Model):
    """A project you worked on (used to answer "tell us about a project / an achievement" questions)."""

    name: str = ""
    company: str = ""
    description: str = ""
    technologies: list[str] = Field(default_factory=list)
    outcome: str = ""  # measurable result, only if true (e.g. "reduced page load time by 40%")


class Skills(_Model):
    primary: list[str] = Field(default_factory=list)
    secondary: list[str] = Field(default_factory=list)
    years: dict[str, float | None] = Field(default_factory=dict)
    # true = primary + secondary list every skill you would claim: "years of experience with <anything else>" is then
    # answered 0 and "do you have experience with <anything else>" No. Unset = those are suggested and left for you.
    complete: bool | None = None

    @property
    def all(self) -> list[str]:
        return list(dict.fromkeys([*self.primary, *self.secondary]))

    def years_for(self, skill: str) -> float | None:
        key = skill.strip().lower()
        for k, v in self.years.items():
            if k.strip().lower() == key:
                return v
        return None


class WorkPreferences(_Model):
    preferred_locations: list[str] = Field(default_factory=list)
    open_to_remote: bool = True
    open_to_hybrid: bool = True
    open_to_onsite: bool = True
    willing_to_relocate: bool = False
    requires_sponsorship: bool = False
    authorized_countries: list[str] = Field(default_factory=lambda: ["India"])
    citizenship: list[str] = Field(default_factory=list)  # countries you are a citizen of (empty = asked when a form needs it)
    willing_to_travel: bool | None = None
    open_to_shifts: bool | None = None
    open_to_contract: bool | None = None  # contract / temporary roles (None = asked when a form wants to know)
    interview_availability: str = ""  # e.g. "Available for interviews on weekdays" (empty = ask each time)


class JobPreferences(_Model):
    target_titles: list[str] = Field(default_factory=list)
    acceptable_titles: list[str] = Field(default_factory=list)
    excluded_title_keywords: list[str] = Field(default_factory=list)
    excluded_companies: list[str] = Field(default_factory=list)
    excluded_jd_keywords: list[str] = Field(default_factory=list)
    min_frontend_ratio: float = 0.5
    accept_fullstack: bool = True
    min_salary_lpa: float | None = None


class SearchConfig(_Model):
    queries: list[str] = Field(default_factory=lambda: ["Frontend Developer"])
    locations: list[str] = Field(default_factory=list)
    portals: list[str] = Field(default_factory=lambda: ["naukri", "linkedin", "instahyre"])
    max_pages_per_query: int = 3
    max_jobs_per_query: int = 50
    max_applications_per_run: int = 15
    max_applications_per_day: int = 40
    posted_within_days: int = 7
    linkedin_easy_apply_only: bool = False
    delay_between_applications_s: list[int] = Field(default_factory=lambda: [15, 40])
    use_experience_filter: bool = True

    @field_validator("portals")
    @classmethod
    def _lower(cls, v: list[str]) -> list[str]:
        return [p.strip().lower() for p in v if p.strip()]


class MatchingConfig(_Model):
    apply_threshold: float = 70
    skip_threshold: float = 45
    use_ai: bool = True
    ai_for_ambiguous_only: bool = True
    experience_tolerance_years: float = 2
    location_strict: bool = True


class ResumeEntry(_Model):
    id: str
    path: str
    label: str = ""
    tags: list[str] = Field(default_factory=list)
    default: bool = False

    @property
    def absolute_path(self) -> Path:
        p = Path(self.path).expanduser()
        return p if p.is_absolute() else (PROJECT_ROOT / p).resolve()


class CustomAnswer(_Model):
    pattern: str  # case-insensitive substring, or regex when prefixed with "re:"
    answer: str


class Diversity(_Model):
    gender: str = ""
    pronouns: str = ""
    veteran_status: str = ""
    disability_status: str = ""
    ethnicity: str = ""


class Profile(_Model):
    profile_confirmed: bool = False
    personal: Personal = Field(default_factory=Personal)
    professional: Professional = Field(default_factory=Professional)
    skills: Skills = Field(default_factory=Skills)
    work_preferences: WorkPreferences = Field(default_factory=WorkPreferences)
    job_preferences: JobPreferences = Field(default_factory=JobPreferences)
    search: SearchConfig = Field(default_factory=SearchConfig)
    matching: MatchingConfig = Field(default_factory=MatchingConfig)
    resumes: list[ResumeEntry] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    custom_answers: list[CustomAnswer] = Field(default_factory=list)
    diversity: Diversity = Field(default_factory=Diversity)

    def facts_for_ai(self) -> dict[str, Any]:
        """Compact, factual view of the profile given to the local LLM."""
        p, pr = self.personal, self.professional
        return {
            "name": p.full_name,
            "location": ", ".join(x for x in [p.current_city, p.country] if x),
            "current_title": pr.current_title,
            "current_company": pr.current_company,
            "total_experience_years": pr.total_experience_years,
            "notice_period_days": pr.notice_period_days,
            "summary": pr.summary,
            "highlights": pr.highlights,
            "primary_skills": self.skills.primary,
            "secondary_skills": self.skills.secondary,
            "skill_years": {k: v for k, v in self.skills.years.items() if v is not None},
            "work_history": [w.model_dump() for w in pr.work_history],
            "education": [e.model_dump() for e in pr.education],
            "certifications": pr.certifications,
            "preferred_locations": self.work_preferences.preferred_locations,
            "open_to_remote": self.work_preferences.open_to_remote,
            "willing_to_relocate": self.work_preferences.willing_to_relocate,
        }


class ProfileIssue(BaseModel):
    field: str
    message: str
    severity: str  # "error" blocks real submissions, "warning" is informational


def is_placeholder(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        v = value.strip()
        return not v or any(m in v for m in PLACEHOLDER_MARKERS)
    return False


def validate_profile(profile: Profile) -> list[ProfileIssue]:
    issues: list[ProfileIssue] = []

    def err(field: str, msg: str) -> None:
        issues.append(ProfileIssue(field=field, message=msg, severity="error"))

    def warn(field: str, msg: str) -> None:
        issues.append(ProfileIssue(field=field, message=msg, severity="warning"))

    if not profile.profile_confirmed:
        err("profile_confirmed", "Review config/profile.json and set profile_confirmed to true "
                                 "before real applications are submitted.")
    p = profile.personal
    for name in ("first_name", "last_name", "email", "phone"):
        if is_placeholder(getattr(p, name)):
            err(f"personal.{name}", f"{name.replace('_', ' ').title()} is missing or a placeholder.")
    if p.email and "@" not in p.email:
        err("personal.email", "Email does not look valid.")
    if is_placeholder(p.current_city):
        warn("personal.current_city", "Current city is not set; location questions will need review.")
    for name in ("linkedin_url", "github_url"):
        if is_placeholder(getattr(p, name)):
            warn(f"personal.{name}", f"{name.replace('_', ' ')} is not set.")

    pr = profile.professional
    if pr.total_experience_years is None:
        err("professional.total_experience_years", "Total experience is required.")
    if pr.notice_period_days is None:
        warn("professional.notice_period_days", "Notice period is not set; notice questions will need review.")
    if pr.current_ctc_lpa is None:
        warn("professional.current_ctc_lpa", "Current CTC not set; salary questions will need review.")
    if pr.expected_ctc_lpa is None:
        warn("professional.expected_ctc_lpa", "Expected CTC not set; salary questions will need review.")
    if is_placeholder(pr.summary):
        warn("professional.summary", "Summary is empty; AI-written answers will be weaker.")
    if not profile.skills.primary:
        err("skills.primary", "At least one primary skill is required for matching.")

    if not profile.resumes:
        err("resumes", "No resumes configured.")
    else:
        existing = [r for r in profile.resumes if r.absolute_path.is_file()]
        if not existing:
            err("resumes", "None of the configured resume files exist. Put your PDF(s) in resumes/.")
        for r in profile.resumes:
            if not r.absolute_path.is_file():
                warn(f"resumes.{r.id}", f"Resume file not found: {r.path}")
    if not profile.search.queries:
        err("search.queries", "At least one search query is required.")
    # contradictions make answers ambiguous ("are you currently employed?", "how soon can you join?")
    from app.answers.question_bank import parse_profile_date

    lwd = parse_profile_date(pr.last_working_day)
    current = [w for w in pr.work_history if (w.end or "present").strip().lower() in {"present", "current", ""}]
    if lwd and lwd < date.today() and current and not pr.serving_notice:
        warn("professional.last_working_day",
             f"Last working day ({pr.last_working_day}) has passed but {current[0].company or 'a role'} is still marked "
             "'present' - set that role's end date (or clear last_working_day); until then 'are you currently employed' "
             "questions are asked.")
    if pr.last_working_day and lwd is None:
        warn("professional.last_working_day", "Last working day is not a recognisable date (use e.g. '5 March 2026').")
    return issues


_DISCOVERY_ONLY = ("search.", "skills.")  # config errors that affect searching/matching, not submitting


def blocking_issues(profile: Profile) -> list[ProfileIssue]:
    """Issues that block real submissions (missing identity / contact facts, no resume, not confirmed)."""
    return [i for i in validate_profile(profile)
            if i.severity == "error" and not i.field.startswith(_DISCOVERY_ONLY)]


class ProfileStore:
    """Loads config/profile.json, reloading automatically when the file changes."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._profile: Profile | None = None
        self._mtime: float | None = None
        self.last_error: str = ""

    @property
    def path(self) -> Path:
        return self._path or get_settings().profile_file

    def ensure_exists(self) -> None:
        if self.path.exists():
            return
        example = PROJECT_ROOT / "config" / "profile.example.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if example.exists():
            shutil.copy(example, self.path)
            log.warning("Created %s from profile.example.json - please fill in your details", self.path)
        else:
            self.path.write_text(Profile().model_dump_json(indent=2), encoding="utf-8")
        self.path.chmod(0o600)  # your details: your account only

    def load(self, force: bool = False) -> Profile:
        with self._lock:
            self.ensure_exists()
            mtime = self.path.stat().st_mtime
            if self._profile is not None and not force and mtime == self._mtime:
                return self._profile
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                profile = Profile.model_validate(raw)
                self.last_error = ""
            except Exception as exc:  # invalid JSON or schema: keep last good profile
                self.last_error = f"Could not load {self.path.name}: {exc}"
                log.error(self.last_error)
                if self._profile is not None:
                    return self._profile
                raise
            self._profile, self._mtime = profile, mtime
            return profile

    def raw(self) -> dict[str, Any]:
        self.ensure_exists()
        return json.loads(self.path.read_text(encoding="utf-8"))

    def update_sections(self, updates: dict[str, Any]) -> Profile:
        """Merge top-level sections into profile.json (validated before writing)."""
        with self._lock:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for key, value in updates.items():
                if isinstance(value, dict) and isinstance(raw.get(key), dict):
                    raw[key].update(value)
                else:
                    raw[key] = value
            profile = Profile.model_validate(raw)  # raises on invalid data
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            tmp.chmod(0o600)
            tmp.replace(self.path)
            self._profile, self._mtime = profile, self.path.stat().st_mtime
            return profile


_store: ProfileStore | None = None


def get_profile_store() -> ProfileStore:
    global _store
    if _store is None:
        _store = ProfileStore()
    return _store


def set_profile_store(store: ProfileStore | None) -> None:
    global _store
    _store = store


def get_profile() -> Profile:
    return get_profile_store().load()
