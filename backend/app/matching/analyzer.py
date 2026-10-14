"""Deterministic job analysis: title category, seniority, skills, FE/BE ratio, experience, location, salary."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.matching.skills import (
    BACKEND_CATEGORIES,
    FRONTEND_CATEGORIES,
    all_skills,
    canonicalize,
    extract_skills,
    skill_def,
)


@dataclass
class JobInput:
    title: str
    description: str = ""
    company: str = ""
    location: str = ""
    experience_text: str = ""
    salary_text: str = ""
    skills: list[str] = field(default_factory=list)  # tags supplied by the portal


@dataclass
class JobAnalysis:
    title_category: str
    title_seniority: str
    skills: dict[str, int]
    required_skills: set[str]
    frameworks: set[str]
    frontend_ratio: float | None
    category_weights: dict[str, float]
    experience_min: float | None
    experience_max: float | None
    locations: list[str]
    work_mode: str
    salary_min_lpa: float | None
    salary_max_lpa: float | None
    jd_length: int
    experience_source: str = ""  # "listing" (the portal's experience field) | "description" | ""
    experience_note: str = ""  # set when the listing's field and the rest of the posting disagree
    experience_conflict: bool = False  # the field says junior, the title says senior, the description is silent

    def to_dict(self) -> dict:
        return {
            "title_category": self.title_category,
            "title_seniority": self.title_seniority,
            "skills": self.skills,
            "required_skills": sorted(self.required_skills),
            "frameworks": sorted(self.frameworks),
            "frontend_ratio": None if self.frontend_ratio is None else round(self.frontend_ratio, 2),
            "category_weights": {k: round(v, 2) for k, v in self.category_weights.items()},
            "experience_min": self.experience_min,
            "experience_max": self.experience_max,
            "experience_source": self.experience_source,
            "experience_note": self.experience_note,
            "experience_conflict": self.experience_conflict,
            "locations": self.locations,
            "work_mode": self.work_mode,
            "salary_min_lpa": self.salary_min_lpa,
            "salary_max_lpa": self.salary_max_lpa,
            "jd_length": self.jd_length,
        }


# ---------------------------------------------------------------- title ---

_TITLE_CATEGORIES: list[tuple[str, str]] = [
    ("fullstack", r"full[\s-]?stack|\bmern\b|\bmean\s+stack\b"),
    ("qa", r"\bqa\b|\bsdet\b|quality\s+(?:assurance|engineer)|\btest(?:ing)?\s+(?:engineer|analyst|lead)\b|\bautomation\s+test|\btester\b"),
    # ("mobile web" / "mobile-first" is the web, seen on a phone)
    ("mobile", r"\bandroid\b|\bios\b|\bmobile\b(?![\s-]*(?:web|first|responsive|friendly))|\bflutter\b|react[\s-]*native|\bswift\b|\bkotlin\b"),
    ("devops", r"\bdevops\b|\bsre\b|site\s+reliability|\bcloud\s+(?:engineer|architect)\b|\bplatform\s+engineer\b|\binfrastructure\b|\bdevsecops\b"),
    ("data", r"\bdata\s+(?:scientist|engineer|analyst)\b|machine\s+learning|\bml\s+engineer\b|\bai\s+engineer\b|\banalytics\b|\bbi\s+developer\b"),
    ("frontend", r"front[\s-]?end|\bui\b|\bux\s+engineer\b|\breact(?:\.?js)?\b|\bangular(?:js)?\b|\bvue(?:\.?js)?\b|\bjavascript\b|\bjs\b|next\.?js|\bweb\s+(?:developer|engineer|ui)\b|\bsvelte\b|\btypescript\b"),
    ("backend", r"back[\s-]?end|\bjava\b(?!\s*script)|\bpython\b|\bgolang\b|\.net\b|\bc#|\bphp\b|\bruby\b|\bnode(?:\.?js)?\b|\bspring\b|\bdjango\b|\bscala\b|\bc\+\+|\bdot\s*net\b|\bserver[\s-]side\b|\bapi\s+developer\b|\bsalesforce\b|\bsap\b|\bmainframe\b"),
]
_GENERIC_TITLE = re.compile(r"\bengineer\b|\bdeveloper\b|\bsde\b|\bprogrammer\b|\bsoftware\b|\bconsultant\b", re.I)

_SENIORITY: list[tuple[str, str]] = [
    ("intern", r"\bintern(?:ship)?\b|\btrainee\b|\bapprentice\b"),
    ("director", r"\bdirector\b|\bvp\b|vice\s+president|\bhead\s+of\b|\bcto\b|\bchief\b"),
    ("manager", r"\bmanager\b"),
    ("principal", r"\bprincipal\b|\bdistinguished\b|\barchitect\b"),
    ("staff", r"\bstaff\b|\bsde[\s-]?(?:iv|4)\b"),
    ("lead", r"\blead\b|\bsde[\s-]?(?:iii|3)\b|\bsoftware\s+engineer\s+(?:iii|3)\b"),
    ("senior", r"\bsenior\b|\bsr\.?(?=\s)|\bsde[\s-]?(?:ii|2)\b|\bsoftware\s+engineer\s+(?:ii|2)\b"),
    ("junior", r"\bjunior\b|\bjr\.?(?=\s)|\bentry[\s-]level\b|\bgraduate\b|\bfresher\b|\bsde[\s-]?(?:i|1)\b|\bassociate\b(?!\s+(?:director|architect|manager))"),
]

SENIORITY_RANK = {"intern": 0, "junior": 1, "mid": 2, "unspecified": 2, "senior": 3, "lead": 4, "staff": 5,
                  "manager": 5, "principal": 6, "director": 7}


# Backend technologies named in a title next to a frontend one ("Java React Developer", ".NET + React",
# "Python React Full Stack"). Heavy stacks are usually backend-dominated roles; light ones are the
# "React + Python / React + Node" combinations a frontend engineer can reasonably take.
_TITLE_BACKEND_HEAVY = {
    "Java": r"\bjava\b(?!\s*script)|\bj2ee\b|\bspring(?:\s*boot)?\b",
    ".NET": r"\.net\b|\bdot\s*net\b|\bdotnet\b|\bc#|\basp\.net\b",
    "PHP": r"\bphp\b|\blaravel\b",
    "Go": r"\bgolang\b",
    "Ruby": r"\bruby\b|\brails\b",
    "Scala": r"\bscala\b",
    "C++": r"\bc\+\+",
    "Salesforce": r"\bsalesforce\b",
}
_TITLE_BACKEND_LIGHT = {
    "Python": r"\bpython\b|\bdjango\b|\bflask\b|\bfastapi\b",
    "Node.js": r"\bnode(?:\.?js)?\b|\bnest\.?js\b|\bexpress(?:\.?js)?\b",  # (after _tidy_title: "nodejs", "nestjs")
}
_TITLE_FRONTEND_TECH = re.compile(
    r"front[\s-]?end|\bui\b|\breact(?:\.?js)?\b(?![\s-]*native)|\bangular(?:js)?\b|\bvue(?:\.?js)?\b|next\.?js|"
    r"\btypescript\b|\bjavascript\b", re.I)


def _tidy_title(title: str) -> str:
    """The title with the spellings that hide a technology from the patterns put right: ". Net Full stack developer"
    (a .NET role that passed the '.net' exclusion and was queued), "Node Js Developer" (whose "Js" read as
    JavaScript made a backend role look like a frontend one)."""
    t = re.sub(r"(?<![\w.])\.\s+net\b", ".net", title or "", flags=re.I)
    return re.sub(r"\b(node|nest|express)[\s.]+js\b", r"\1js", t, flags=re.I)


def title_backend_stack(title: str) -> tuple[list[str], list[str]]:
    """(heavy, light) backend technologies named in a job title."""
    title = _tidy_title(title)
    t = title or ""
    heavy = [name for name, rx in _TITLE_BACKEND_HEAVY.items() if re.search(rx, t, re.I)]
    light = [name for name, rx in _TITLE_BACKEND_LIGHT.items() if re.search(rx, t, re.I)]
    return heavy, light


def normalize_title_aliases(title: str) -> str:
    """Spelling variants used by recruiters: 'Dot Net' -> '.net', 'Back-End' -> 'backend', ..."""
    t = _tidy_title(title)
    t = re.sub(r"\bdot\s*net\b|\bdotnet\b", ".net", t, flags=re.I)
    t = re.sub(r"\bback[\s-]+end\b", "backend", t, flags=re.I)
    t = re.sub(r"\bfront[\s-]+end\b", "frontend", t, flags=re.I)
    t = re.sub(r"\bfull[\s-]+stack\b", "fullstack", t, flags=re.I)
    return t


def classify_title(title: str) -> str:
    t = _tidy_title(title)
    says_frontend = bool(re.search(r"front[\s-]?end", t, re.I))
    for cat, pattern in _TITLE_CATEGORIES:
        if cat == "data" and says_frontend:
            continue  # "Senior Frontend Developer (Analytics)" names a domain, not a data role
        if re.search(pattern, t, re.I):
            if cat == "frontend" and any(title_backend_stack(t)):
                return "fullstack"  # "Java React Developer", ".NET + React", "React + Node Developer"
            return cat
    if _GENERIC_TITLE.search(t):
        return "generic"
    return "other"


def title_seniority(title: str) -> str:
    for level, pattern in _SENIORITY:
        if re.search(pattern, title or "", re.I):
            return level
    return "unspecified"


def seniority_for_years(years: float | None) -> str:
    if years is None:
        return "mid"
    if years < 1:
        return "intern"
    if years < 2.5:
        return "junior"
    if years < 5:
        return "mid"
    if years < 9:
        return "senior"
    if years < 12:
        return "lead"
    return "staff"


# ----------------------------------------------------------- experience ---

_NUM = r"(\d{1,2}(?:\.\d)?)"
_YRS = r"\s*\+?\s*(?:years?|yrs?|yoe\b)"
_EXP_RANGE = re.compile(_NUM + r"\s*(?:-|–|—|to)\s*" + _NUM + _YRS, re.I)
# "Experience: 2 3 Years" - a range whose dash was lost when the posting was pasted
_EXP_RANGE_SPACED = re.compile(r"(?<![\d.])" + _NUM + r"\s+" + _NUM + _YRS, re.I)
_EXP_PLUS = re.compile(_NUM + r"\s*\+\s*(?:years?|yrs?|yoe\b)", re.I)
_EXP_MIN = re.compile(r"(?:minimum|min\.?|at\s*least|atleast|over)\s*(?:of\s*)?" + _NUM + _YRS, re.I)
_EXP_SIMPLE = re.compile(
    _NUM + r"\s*(?:years?|yrs?)\s*(?:of\s+)?(?:overall\s+|total\s+|relevant\s+|professional\s+|hands[\s-]on\s+|"
           r"industry\s+|work\s+|strong\s+|solid\s+)?(?:experience|exp\b)", re.I)
_FRESHER = re.compile(r"\bfreshers?\b", re.I)
# years that describe the employer, not the candidate: "Established company with 10+ years of industry presence",
# "a 25-year legacy", "serving clients for over 15 years"
_EMPLOYER_YEARS_AFTER = re.compile(r"^\W*(?:of\s+)?(?:industry\s+presence|presence|in\s+(?:business|the\s+(?:industry|market|field))|"
                                   r"legacy|history|excellence|track\s+record|trust|of\s+service|in\s+operation|"
                                   r"of\s+operations?|old\b)", re.I)
_EMPLOYER_YEARS_BEFORE = re.compile(r"(?:compan(?:y|ies)|organi[sz]ation|firm|agency|established|founded|legacy\s+of|"
                                    r"history\s+of|clients?\s+for|customers?\s+for|in\s+business\s+for)[ \t,:-]+(?:[\w+]+[ \t,]+){0,3}$", re.I)


def _about_the_employer(text: str, start: int, end: int) -> bool:
    return bool(_EMPLOYER_YEARS_AFTER.search(text[end:end + 40]) or _EMPLOYER_YEARS_BEFORE.search(text[max(0, start - 60):start]))


def parse_experience(text: str) -> tuple[float | None, float | None]:
    """Parse '5-8 Yrs', '5+ years', 'minimum 3 years' ... into (min, max). Years that describe the employer
    ("10+ years of industry presence") are not a requirement; the end of a range ("2 to 5 years of experience") is
    not a second, higher one."""
    if not text:
        return None, None
    candidates: list[tuple[float, float | None]] = []
    spans: list[tuple[int, int]] = []
    for rx in (_EXP_RANGE, _EXP_RANGE_SPACED):
        for m in rx.finditer(text):
            lo, hi = float(m.group(1)), float(m.group(2))
            spaced = rx is _EXP_RANGE_SPACED
            if any(a <= m.start() < b for a, b in spans) or _about_the_employer(text, m.start(), m.end()):
                continue
            if lo <= hi <= 40 and not (spaced and (lo == hi or hi - lo > 6 or hi > 20)):
                candidates.append((lo, hi))
                spans.append(m.span())
    for rx in (_EXP_PLUS, _EXP_MIN, _EXP_SIMPLE):
        for m in rx.finditer(text):
            if any(a <= m.start(1) < b for a, b in spans) or _about_the_employer(text, m.start(), m.end()):
                continue
            lo = float(m.group(1))
            if lo <= 30 and not any(c[0] == lo for c in candidates):
                candidates.append((lo, None))
    if not candidates:
        if _FRESHER.search(text) and len(text) < 40:
            return 0.0, 1.0
        return None, None
    # the overall requirement is normally the most demanding one
    lo, hi = max(candidates, key=lambda c: (c[0], c[1] or 0))
    return lo, hi


def _span(lo: float | None, hi: float | None) -> str:
    return f"{lo or 0:g}-{hi:g}" if hi is not None else f"{lo or 0:g}+"


def posting_experience(job: JobInput) -> tuple[float | None, float | None, str, str, bool]:
    """(min, max, source, note, conflict): the experience a posting asks for.

    The portal's own experience field comes first, the description when that field is empty. Recruiters often leave
    the field at a junior default ("0-2 Yrs" on Naukri) for senior roles - 'Senior Frontend Software Engineer',
    field "0-2 Yrs", description "5+ years' experience in HTML, CSS, JS, React". So a junior range in the field is
    not taken at face value when the rest of the posting says otherwise:

    * the description states clearly more (at least 2 years above the field's maximum) and the title is not a
      junior one -> the description's figure is used;
    * only the title disagrees (Senior / Lead / Staff / Principal) -> the field is kept and the posting is marked
      as conflicting, which sends it to your review instead of skipping it."""
    lo, hi = parse_experience(job.experience_text)
    jd_lo, jd_hi = parse_experience(job.description)
    if lo is None:
        return jd_lo, jd_hi, ("description" if jd_lo is not None else ""), "", False
    if hi is None or hi > 3:
        return lo, hi, "listing", "", False
    level = title_seniority(job.title)
    if level in {"intern", "junior"}:
        return lo, hi, "listing", "", False
    if jd_lo is not None and jd_lo >= hi + 2:
        note = (f"The listing's experience field says {_span(lo, hi)} years but the description asks for "
                f"{_span(jd_lo, jd_hi)}: the description is used")
        return jd_lo, jd_hi, "description", note, False
    if SENIORITY_RANK.get(level, 0) >= SENIORITY_RANK["senior"]:
        note = (f"The listing's experience field says {_span(lo, hi)} years but the title says "
                f"'{level}' - one of them is wrong")
        return lo, hi, "listing", note, True
    return lo, hi, "listing", "", False


# ------------------------------------------------------------- location ---

CITY_ALIASES: dict[str, list[str]] = {
    "bengaluru": ["bengaluru", "bangalore", "blr", "bengalooru", "banglore"],
    "mumbai": ["mumbai", "bombay", "navi mumbai", "thane"],
    "delhi ncr": ["delhi ncr", "delhi", "new delhi", "ncr", "noida", "greater noida", "gurgaon", "gurugram",
                  "ghaziabad", "faridabad"],
    "hyderabad": ["hyderabad", "secunderabad", "hyd"],
    "chennai": ["chennai", "madras"],
    "pune": ["pune"],
    "kolkata": ["kolkata", "calcutta"],
    "ahmedabad": ["ahmedabad", "gandhinagar"],
    "kochi": ["kochi", "cochin", "ernakulam"],
    "thiruvananthapuram": ["thiruvananthapuram", "trivandrum"],
    "jaipur": ["jaipur"],
    "chandigarh": ["chandigarh", "mohali", "panchkula"],
    "indore": ["indore"],
    "coimbatore": ["coimbatore"],
    "mysuru": ["mysuru", "mysore"],
    "nagpur": ["nagpur"],
    "bhubaneswar": ["bhubaneswar"],
    "lucknow": ["lucknow"],
    "vadodara": ["vadodara", "baroda"],
    "surat": ["surat"],
    "visakhapatnam": ["visakhapatnam", "vizag"],
    "mangaluru": ["mangaluru", "mangalore"],
    "goa": ["goa", "panaji"],
    "singapore": ["singapore"],
    "dubai": ["dubai"],
    "london": ["london"],
}
_REMOTE = re.compile(r"\bremote\b|work\s+from\s+home|\bwfh\b|\bwork\s+from\s+anywhere\b", re.I)
_HYBRID = re.compile(r"\bhybrid\b", re.I)
_ANYWHERE = re.compile(r"\bpan\s+india\b|\banywhere\s+in\s+india\b|\ball\s+india\b|\bmultiple\s+locations\b", re.I)


def normalize_city(text: str) -> str | None:
    t = (text or "").strip().lower()
    if not t:
        return None
    for canon, aliases in CITY_ALIASES.items():
        for a in aliases:
            if re.search(rf"(?<![a-z]){re.escape(a)}(?![a-z])", t):
                return canon
    return None


def parse_locations(text: str) -> list[str]:
    found: list[str] = []
    low = (text or "").lower()
    for canon, aliases in CITY_ALIASES.items():
        if any(re.search(rf"(?<![a-z]){re.escape(a)}(?![a-z])", low) for a in aliases):
            found.append(canon)
    if _REMOTE.search(text or ""):
        found.append("remote")
    if _ANYWHERE.search(text or ""):
        found.append("anywhere")
    return found


def parse_work_mode(location: str, description: str) -> str:
    if _REMOTE.search(location or ""):
        return "remote"
    if _HYBRID.search(location or ""):
        return "hybrid"
    head = (description or "")[:1500]
    if re.search(r"\b(?:fully|100%)\s+remote\b|\bremote[\s-](?:first|only)\b", head, re.I):
        return "remote"
    if _HYBRID.search(head):
        return "hybrid"
    if re.search(r"\bwork\s+from\s+office\b|\bwfo\b|\bon[\s-]?site\b|\bin[\s-]office\b", head, re.I):
        return "onsite"
    return "unknown"


# --------------------------------------------------------------- salary ---

_SAL_LPA = re.compile(r"(\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(\d+(?:\.\d+)?)\s*(?:lacs?|lakhs?|lpa|l\b|lakh\s+p\.?a)", re.I)
_SAL_RUPEES = re.compile(r"(\d{1,3}(?:,\d{2,3})+)\s*(?:-|–|to)\s*(\d{1,3}(?:,\d{2,3})+)")


def parse_salary_lpa(text: str) -> tuple[float | None, float | None]:
    if not text or re.search(r"not\s+disclosed", text, re.I):
        return None, None
    m = _SAL_LPA.search(text)
    if m:
        return float(m.group(1)), float(m.group(2))
    m = _SAL_RUPEES.search(text)
    if m:
        lo = float(m.group(1).replace(",", "")) / 100_000
        hi = float(m.group(2).replace(",", "")) / 100_000
        if 1 <= lo <= hi <= 500:
            return round(lo, 1), round(hi, 1)
    return None, None


# ------------------------------------------------------- JD sections ---

_REQUIRED_HDR = re.compile(
    r"must[\s-]have|required|requirements|qualifications|what\s+you(?:'ll|\s+will)?\s+need|"
    r"looking\s+for|skills\s*(?:&|and)?\s*(?:required|needed|experience)|mandatory|key\s+skills|"
    r"you\s+have|you\s+bring|who\s+you\s+are|primary\s+skills|technical\s+skills|desired\s+(?:candidate\s+)?profile",
    re.I)
_OPTIONAL_HDR = re.compile(
    r"nice[\s-]to[\s-]have|good[\s-]to[\s-]have|preferred|bonus|desirable|plus\s+points|added\s+advantage|"
    r"secondary\s+skills|optional", re.I)
_OTHER_HDR = re.compile(
    r"responsibilities|what\s+you(?:'ll|\s+will)\s+do|about\s+(?:us|the\s+(?:company|team|role))|benefits|perks|"
    r"why\s+join|what\s+we\s+offer|role\s+overview|job\s+summary|company\s+overview", re.I)


def _is_header(line: str) -> bool:
    s = line.strip()
    return 0 < len(s) <= 70 and (s.endswith(":") or s.isupper() or len(s.split()) <= 6)


def required_skills_from_sections(description: str) -> tuple[set[str], set[str], bool]:
    """Returns (required, optional, sections_found)."""
    required: set[str] = set()
    optional: set[str] = set()
    section = "none"
    found_sections = False
    for line in (description or "").splitlines():
        stripped = line.strip(" \t-•*#")
        if _is_header(stripped):
            if _OPTIONAL_HDR.search(stripped):
                section, found_sections = "optional", True
                continue
            if _REQUIRED_HDR.search(stripped):
                section, found_sections = "required", True
                # header lines like "Required: React, TypeScript" carry skills too
                if ":" in stripped:
                    required |= set(extract_skills(stripped.split(":", 1)[1]))
                continue
            if _OTHER_HDR.search(stripped):
                section, found_sections = "other", True
                continue
        skills = set(extract_skills(stripped))
        if section == "required":
            required |= skills
        elif section == "optional":
            optional |= skills
    return required, optional - required, found_sections


# ------------------------------------------------------------- analysis ---

_TITLE_PRIOR = {"frontend": 0.95, "fullstack": 0.5, "generic": 0.5, "other": 0.4, "backend": 0.1,
                "mobile": 0.1, "devops": 0.05, "data": 0.05, "qa": 0.2}


def analyze_job(job: JobInput) -> JobAnalysis:
    text = f"{job.title}\n{job.description}"
    skills = extract_skills(text)
    for tag in job.skills:
        canon = canonicalize(tag)
        if skill_def(canon):
            skills[canon] = skills.get(canon, 0) + 1

    required, _optional, sections = required_skills_from_sections(job.description)
    tag_skills = {canonicalize(t) for t in job.skills if skill_def(canonicalize(t))}
    title_skills = set(extract_skills(job.title))
    if not sections:
        required = {s for s, n in skills.items() if n >= 2}
    required |= title_skills | tag_skills
    signal_only = {s.name for s in all_skills() if s.signal_only}
    required -= signal_only

    weights: dict[str, float] = {}
    for name, count in skills.items():
        d = skill_def(name)
        if not d:
            continue
        w = d.weight * min(count, 3)
        weights[d.category] = weights.get(d.category, 0.0) + w

    fe = sum(v for k, v in weights.items() if k in FRONTEND_CATEGORIES)
    fe -= 0.5 * weights.get("testing", 0.0)  # testing tools are only a weak frontend signal
    be = sum(v for k, v in weights.items() if k in BACKEND_CATEGORIES) + 0.8 * weights.get("fullstack_js", 0.0)
    category = classify_title(job.title)
    prior = _TITLE_PRIOR.get(category, 0.5)
    if fe + be > 0:
        ratio = 0.7 * (fe / (fe + be)) + 0.3 * prior
    else:
        ratio = prior if category in _TITLE_PRIOR and category not in {"generic", "other"} else None

    exp_lo, exp_hi, exp_source, exp_note, exp_conflict = posting_experience(job)

    sal_lo, sal_hi = parse_salary_lpa(job.salary_text)
    frameworks = {s for s in skills if (d := skill_def(s)) and d.framework}

    return JobAnalysis(
        title_category=category,
        title_seniority=title_seniority(job.title),
        skills={k: v for k, v in sorted(skills.items(), key=lambda kv: -kv[1])},
        required_skills=required,
        frameworks=frameworks,
        frontend_ratio=None if ratio is None else max(0.0, min(1.0, ratio)),
        category_weights=weights,
        experience_min=exp_lo,
        experience_max=exp_hi,
        experience_source=exp_source,
        experience_note=exp_note,
        experience_conflict=exp_conflict,
        locations=parse_locations(job.location) or parse_locations(job.description[:600]),
        work_mode=parse_work_mode(job.location, job.description),
        salary_min_lpa=sal_lo,
        salary_max_lpa=sal_hi,
        jd_length=len(job.description or ""),
    )
