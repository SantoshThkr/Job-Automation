"""Deterministic answers to common application questions, taken only from profile facts.

A handler returns:
* ``Answer`` with a value  -> the profile contains the fact
* ``Answer`` with value None -> the question was recognised but the fact is missing (manual review)
* ``OPEN_ENDED``            -> free-text question that needs writing (summary / AI)
* ``None``                  -> not recognised
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.answers.options import choose_decline
from app.matching.analyzer import JobInput, normalize_city, parse_locations, seniority_for_years
from app.matching.skills import canonicalize, extract_skills, skill_def
from app.profile import Profile


@dataclass
class QuestionContext:
    question: str
    field_type: str = "text"  # text|textarea|number|select|radio|checkbox|date|email|tel|url
    options: list[str] = field(default_factory=list)
    required: bool = False
    max_length: int | None = None
    job: JobInput | None = None
    portal: str = ""


@dataclass
class Answer:
    value: str | None
    confidence: float
    source: str
    needs_review: bool = False
    note: str = ""
    numeric: float | None = None  # helps choose among range options
    yes_no: bool | None = None
    unit_scale: float = 1.0  # option values are in units of (value / unit_scale)
    open_ended: bool = False


OPEN_ENDED = Answer(None, 0.0, "open_ended", open_ended=True)

PORTAL_NAMES = {"naukri": "Naukri.com", "linkedin": "LinkedIn", "instahyre": "Instahyre", "mock": "Job portal"}


def normalize_question(q: str) -> str:
    q = re.sub(r"\(\s*(?:required|optional|mandatory)\s*\)", " ", q or "", flags=re.I)
    q = re.sub(r"[*:?]+", " ", q)
    return re.sub(r"\s+", " ", q).strip().lower()


def fmt_num(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:.1f}".rstrip("0").rstrip(".")


def _yn(value: bool, conf: float, note: str = "") -> Answer:
    return Answer("Yes" if value else "No", conf, "profile", yes_no=value, note=note)


def _missing(fact: str) -> Answer:
    return Answer(None, 0.0, "profile", needs_review=True, note=f"profile.{fact} is not set")


def _is_numeric_field(ctx: QuestionContext) -> bool:
    return ctx.field_type == "number"


def _wants_yes_no(ctx: QuestionContext) -> bool:
    if ctx.field_type == "checkbox":
        return True
    opts = {o.strip().lower() for o in ctx.options}
    return bool(opts) and ("yes" in opts or "no" in opts)


# ------------------------------------------------------------ handlers ---

Handler = Callable[[QuestionContext, Profile, str], "Answer | None"]


def _personal(attr: str, label: str) -> Handler:
    def h(ctx: QuestionContext, p: Profile, q: str) -> Answer | None:
        v = getattr(p.personal, attr)
        return Answer(v, 0.99, "profile") if v and "YOUR_" not in v else _missing(f"personal.{attr}")
    h.__name__ = f"h_{label}"
    return h


def h_full_name(ctx, p, q):
    return Answer(p.personal.full_name, 0.99, "profile") if p.personal.first_name else _missing("personal.first_name")


def h_phone(ctx, p, q):
    if not p.personal.phone or "X" in p.personal.phone:
        return _missing("personal.phone")
    national = p.personal.phone_national
    # Forms with a separate country-code selector (LinkedIn "Mobile phone number", tel inputs) want the
    # national number only
    if re.search(r"without\s+(?:country\s+)?code|10[\s-]digit|\bmobile\b", q) or ctx.field_type in {"tel", "number"}:
        return Answer(national, 0.97, "profile")
    return Answer(p.personal.phone, 0.97, "profile", note="full international format")


def h_country_code(ctx, p, q):
    cc = p.personal.phone_country_code
    if not cc:
        return _missing("personal.phone_country_code")
    if ctx.options:
        digits = cc.lstrip("+")
        for o in ctx.options:
            if re.search(rf"\+?\b{digits}\b", o) and (p.personal.country.lower() in o.lower() or "+" in o):
                return Answer(o, 0.97, "profile")
    return Answer(cc, 0.97, "profile")


def h_city(ctx, p, q):
    city = p.personal.current_city
    if not city:
        return _missing("personal.current_city")
    return Answer(city, 0.97, "profile")


def h_preferred_location(ctx, p, q):
    locs = p.work_preferences.preferred_locations
    if not locs:
        return _missing("work_preferences.preferred_locations")
    return Answer(", ".join(locs), 0.93, "profile")


def h_salary(kind: str) -> Handler:
    def h(ctx: QuestionContext, p: Profile, q: str) -> Answer | None:
        lpa = p.professional.current_ctc_lpa if kind == "current" else p.professional.expected_ctc_lpa
        if lpa is None:
            return _missing(f"professional.{kind}_ctc_lpa")
        if ctx.options:
            # option ranges are usually in lakhs ("10-15 LPA"); rupee amounts ("10,00,000") are rescaled
            sample = re.sub(r"(?<=\d),(?=\d)", "", " ".join(ctx.options))
            scale = 1 / 100_000 if re.search(r"\d{6,}", sample) else 1.0
            return Answer(f"{fmt_num(lpa)} LPA", 0.9, "profile", numeric=lpa, unit_scale=scale)
        if re.search(r"lakh|lac|\blpa\b|\bin\s+l\b|lakhs", q):
            return Answer(fmt_num(lpa), 0.97, "profile", numeric=lpa)
        if re.search(r"per\s+month|monthly", q):
            return Answer(str(int(round(lpa * 100_000 / 12))), 0.92, "profile")
        if re.search(r"\binr\b|rupees|₹|\brs\.?\b|annual|per\s+annum|\bp\.?a\.?\b|absolute|in\s+numbers", q):
            return Answer(str(int(round(lpa * 100_000))), 0.92, "profile")
        if _is_numeric_field(ctx):
            return Answer(str(int(round(lpa * 100_000))), 0.6, "profile", needs_review=True,
                          note="unit not stated; assumed annual INR")
        suffix = " (negotiable)" if kind == "expected" and p.professional.salary_negotiable else ""
        return Answer(f"{fmt_num(lpa)} LPA{suffix}", 0.95, "profile", numeric=lpa)
    h.__name__ = f"h_salary_{kind}"
    return h


def h_salary_currency(ctx, p, q):
    return Answer("INR", 0.9, "profile")


def h_notice(ctx, p, q):
    days = p.professional.notice_period_days
    if days is None:
        return _missing("professional.notice_period_days")
    if ctx.field_type == "date" or re.search(r"\bdate\b", q):
        start = date.today() + timedelta(days=days)
        return Answer(start.isoformat(), 0.88, "profile", note=f"today + {days} days notice")
    if ctx.options:
        opts_low = " ".join(ctx.options).lower()
        scale = 30.0 if "month" in opts_low and "day" not in opts_low else 1.0
        picked = _choose_notice_option(days, ctx.options)
        if picked:
            return Answer(picked, 0.95, "profile")
        return Answer(fmt_num(days / scale), 0.6, "profile", needs_review=True, note="no matching notice option")
    if _is_numeric_field(ctx) or re.search(r"\bin\s+(?:days|months)\b|number\s+of\s+days", q):
        if re.search(r"month", q):
            return Answer(fmt_num(round(days / 30, 1)), 0.95, "profile")
        return Answer(str(days), 0.97, "profile")
    if _wants_yes_no(ctx):
        # e.g. "Can you join immediately?" / "Are you serving notice period?"
        if re.search(r"serving", q):
            return _yn(p.professional.serving_notice, 0.95)
        if re.search(r"immediate", q):
            return _yn(days <= 15, 0.9)
        m = re.search(r"within\s+(\d+)\s*(days|months?)", q)
        if m:
            limit = int(m.group(1)) * (30 if m.group(2).startswith("month") else 1)
            return _yn(days <= limit, 0.93)
        return None
    if days == 0:
        return Answer("Immediate", 0.97, "profile")
    txt = f"{days} days"
    if p.professional.serving_notice and p.professional.last_working_day:
        txt += f" (serving notice, last working day {p.professional.last_working_day})"
    return Answer(txt, 0.96, "profile")


def _choose_notice_option(days: int, options: list[str]) -> str | None:
    def rng(o: str) -> tuple[float, float] | None:
        s = o.lower()
        if re.search(r"immediate|^\s*0\s*days?\s*$", s):
            return 0, 0
        if "serving" in s:
            return None
        unit = 30 if "month" in s else 1
        m = re.search(r"(\d+)\s*(?:-|to)\s*(\d+)", s)
        if m:
            return int(m.group(1)) * unit, int(m.group(2)) * unit
        m = re.search(r"(?:more\s+than|over|above|beyond|>)\s*(\d+)", s)
        if m:
            return int(m.group(1)) * unit + 1, 10_000
        m = re.search(r"(\d+)", s)
        if m:
            n = int(m.group(1)) * unit
            if re.search(r"less|within|up\s*to|upto|or\s+less|<", s):
                return 0, n
            return n, n
        return None

    parsed = [(o, r) for o in options if (r := rng(o)) is not None]
    containing = [(o, r) for o, r in parsed if r[0] <= days <= r[1]]
    if containing:
        return min(containing, key=lambda x: x[1][1] - x[1][0])[0]
    above = [(o, r) for o, r in parsed if r[1] >= days]
    if above:
        return min(above, key=lambda x: x[1][1])[0]
    return None


def _question_skills(q: str) -> list[str]:
    return [s for s in extract_skills(q) if (d := skill_def(s)) and not d.signal_only]


def h_experience(ctx, p, q):
    total = p.professional.total_experience_years
    months = bool(re.search(r"\bmonths?\b", q)) and not re.search(r"years?\s+and\s+months", q)
    skills = _question_skills(q)
    yes_no = _wants_yes_no(ctx)
    if skills and not re.search(r"total|overall", q):
        values = []
        for s in skills:
            canon = canonicalize(s)
            listed = canon in {canonicalize(x) for x in p.skills.all}
            yrs = p.skills.years_for(canon)
            if yrs is None:
                # also accept years keyed by alias
                yrs = next((v for k, v in p.skills.years.items() if canonicalize(k) == canon), None)
            if yrs is not None:
                values.append(yrs)
            elif yes_no:
                values.append(1.0 if listed else 0.0)
            elif not listed:
                return Answer("0", 0.7, "profile", needs_review=True, numeric=0.0,
                              note=f"'{canon}' is not in your skills; confirm 0 years")
            else:
                return _missing(f"skills.years['{canon}']")
        yrs = min(values)
        if yes_no:
            return _yn(yrs > 0, 0.9, note=f"skills: {', '.join(skills)}")
        v = yrs * 12 if months else yrs
        return Answer(fmt_num(v), 0.95, "profile", numeric=yrs, note=f"skill years for {', '.join(skills)}")
    if re.search(r"relevant|front[\s-]?end|ui\s+development|web\s+development", q):
        yrs = p.professional.relevant_experience_years or None
        if yrs is None:
            return _missing("professional.relevant_experience_years")
    else:
        yrs = total
    if yrs is None:
        return _missing("professional.total_experience_years")
    if yes_no:
        m = re.search(r"(\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?)", q)
        if m:
            return _yn(yrs >= float(m.group(1)), 0.95)
        return _yn(yrs > 0, 0.85)
    v = yrs * 12 if months else yrs
    return Answer(fmt_num(v), 0.99, "profile", numeric=yrs)


def h_experience_level(ctx, p, q):
    if not ctx.options:
        return None
    level = seniority_for_years(p.professional.total_experience_years)
    want = {"senior": r"senior|mid[\s-]?senior|experienced", "lead": r"lead|senior",
            "mid": r"mid|intermediate|associate", "junior": r"junior|entry", "staff": r"staff|principal|senior",
            "intern": r"intern|entry"}[level]
    for o in ctx.options:
        if re.search(want, o, re.I):
            return Answer(o, 0.8, "derived", note=f"seniority from {p.professional.total_experience_years} years")
    return None


def h_relocate(ctx, p, q):
    wp = p.work_preferences
    pref = {c for x in wp.preferred_locations if (c := normalize_city(x))}
    current = normalize_city(p.personal.current_city)
    cities = [c for c in parse_locations(q) if c not in {"remote", "anywhere"}]
    if cities and all(c in pref or c == current for c in cities):
        return _yn(True, 0.95, note="location is already one of your preferred cities")
    return _yn(wp.willing_to_relocate, 0.9)


def h_onsite(ctx, p, q):
    wp = p.work_preferences
    pref = {c for x in wp.preferred_locations if (c := normalize_city(x))}
    current = normalize_city(p.personal.current_city)
    cities = [c for c in parse_locations(q) if c not in {"remote", "anywhere"}]
    if cities and not all(c in pref or c == current for c in cities) and not wp.willing_to_relocate:
        return _yn(False, 0.85, note="office city is outside your preferred locations")
    if re.search(r"hybrid", q):
        return _yn(wp.open_to_hybrid, 0.92)
    if re.search(r"remote(?:ly)?\b", q) and not re.search(r"office|on[\s-]?site", q):
        return _yn(wp.open_to_remote, 0.92)
    return _yn(wp.open_to_onsite, 0.92)


def h_sponsorship(ctx, p, q):
    return _yn(p.work_preferences.requires_sponsorship, 0.95)


def h_authorization(ctx, p, q):
    countries = [c.lower() for c in p.work_preferences.authorized_countries]
    if not countries:
        return _missing("work_preferences.authorized_countries")
    known = ["india", "united states", "usa", "u.s.", "us", "united kingdom", "uk", "canada", "germany",
             "singapore", "australia", "uae", "netherlands", "ireland", "european union", "eu"]
    mentioned = [k for k in known if re.search(rf"(?<![a-z]){re.escape(k)}(?![a-z])", q)]
    if mentioned:
        alias = {"usa": "united states", "us": "united states", "u.s.": "united states", "uk": "united kingdom"}
        ok = any(alias.get(m, m) in countries or m in countries for m in mentioned)
        return _yn(ok, 0.93)
    job_loc = (ctx.job.location if ctx.job else "") or ""
    if "india" in countries and (not job_loc or normalize_city(job_loc) or "india" in job_loc.lower()
                                 or "remote" in job_loc.lower()):
        return _yn(True, 0.88, note="job is in India and you are authorized to work in India")
    return Answer(None, 0.0, "profile", needs_review=True, note="work authorization for this country unknown")


def h_adult(ctx, p, q):
    yrs = p.professional.total_experience_years
    if yrs is not None and yrs >= 4:
        return _yn(True, 0.95, note="implied by your years of professional experience")
    return Answer(None, 0.0, "profile", needs_review=True, note="age not in profile")


_DEGREE_LEVEL = [(3, r"ph\.?\s?d|doctor"), (2, r"\bm\.?\s?(?:tech|e|sc|s|ca|ba)\b|master|\bmca\b|\bmba\b|post\s?grad"),
                 (1, r"\bb\.?\s?(?:tech|e|sc|s|ca|ba|com)\b|bachelor|graduat|undergrad"), (0, r"diploma|12th|hsc|high\s+school")]


def _degree_level(text: str) -> int | None:
    for lvl, rx in _DEGREE_LEVEL:
        if re.search(rx, text, re.I):
            return lvl
    return None


def _highest_education(p: Profile):
    eds = [e for e in p.professional.education if e.degree]
    if not eds:
        return None
    return max(eds, key=lambda e: (_degree_level(e.degree) or 0, e.end_year or 0))


def h_degree(ctx, p, q):
    ed = _highest_education(p)
    if ed is None:
        return _missing("professional.education")
    if _wants_yes_no(ctx):
        asked = _degree_level(q)
        mine = _degree_level(ed.degree)
        if asked is not None and mine is not None:
            return _yn(mine >= asked, 0.93)
        return None
    if ctx.options:
        lvl = _degree_level(ed.degree)
        names = {3: r"doctor|ph\.?d", 2: r"master|post\s?grad|m\.?tech|mca|mba", 1: r"bachelor|graduate|b\.?tech|b\.?e\b|undergrad",
                 0: r"diploma|high\s+school"}
        if lvl is not None:
            for o in ctx.options:
                if re.search(names[lvl], o, re.I):
                    return Answer(o, 0.93, "profile")
    text = ed.degree + (f" in {ed.field}" if ed.field else "")
    return Answer(text, 0.95, "profile")


def h_institution(ctx, p, q):
    ed = _highest_education(p)
    return Answer(ed.institution, 0.95, "profile") if ed and ed.institution else _missing("professional.education[].institution")


def h_grad_year(ctx, p, q):
    ed = _highest_education(p)
    return Answer(str(ed.end_year), 0.95, "profile", numeric=ed.end_year) if ed and ed.end_year else _missing("professional.education[].end_year")


def h_grade(ctx, p, q):
    ed = _highest_education(p)
    return Answer(ed.grade, 0.93, "profile") if ed and ed.grade else _missing("professional.education[].grade")


def h_field_of_study(ctx, p, q):
    ed = _highest_education(p)
    return Answer(ed.field, 0.93, "profile") if ed and ed.field else _missing("professional.education[].field")


def h_current_company(ctx, p, q):
    c = p.professional.current_company
    return Answer(c, 0.97, "profile") if c else _missing("professional.current_company")


def h_current_title(ctx, p, q):
    t = p.professional.current_title
    return Answer(t, 0.97, "profile") if t else _missing("professional.current_title")


def h_employed(ctx, p, q):
    if p.professional.current_company:
        return _yn(True, 0.9)
    return _missing("professional.current_company")


def h_english(ctx, p, q):
    if not any(lang.lower() == "english" for lang in p.professional.languages):
        return _missing("professional.languages")
    if _wants_yes_no(ctx):
        return _yn(True, 0.9)
    return Answer("Fluent", 0.6, "profile", needs_review=True, note="proficiency level not in profile")


def _diversity(attr: str) -> Handler:
    def h(ctx: QuestionContext, p: Profile, q: str) -> Answer | None:
        v = getattr(p.diversity, attr)
        if v:
            return Answer(v, 0.95, "profile")
        decline = choose_decline(ctx.options)
        if decline:
            return Answer(decline, 0.9, "derived", note="not in profile - declined to self-identify")
        if not ctx.required:
            return Answer("", 0.9, "derived", note="optional EEO question left blank")
        return _missing(f"diversity.{attr}")
    h.__name__ = f"h_div_{attr}"
    return h


def h_source(ctx, p, q):
    portal = PORTAL_NAMES.get(ctx.portal, ctx.portal.title() if ctx.portal else "")
    if ctx.options:
        for o in ctx.options:
            if portal and portal.split(".")[0].lower() in o.lower():
                return Answer(o, 0.92, "derived")
        for o in ctx.options:
            if re.search(r"job\s*(?:board|portal|site)|online|internet|website", o, re.I):
                return Answer(o, 0.85, "derived")
        return None
    return Answer(portal or "Job portal", 0.9, "derived") if portal else None


def h_consent(ctx, p, q):
    if ctx.field_type in {"checkbox", "radio", "select"} or _wants_yes_no(ctx):
        return Answer("Yes", 0.9, "derived", yes_no=True, note="application consent / accuracy confirmation")
    return None


def h_referral(ctx, p, q):
    if _wants_yes_no(ctx):
        return _yn(False, 0.85, note="applying directly through a portal")
    if not ctx.required:
        return Answer("", 0.9, "derived", note="no referral")
    return Answer(None, 0.0, "profile", needs_review=True, note="referral details not in profile")


def h_previous_employment(ctx, p, q):
    company = (ctx.job.company if ctx.job else "").lower().strip()
    history = [w.company.lower() for w in p.professional.work_history if w.company]
    if re.search(r"applied|interview", q):
        return Answer(None, 0.0, "profile", needs_review=True, note="previous applications are not tracked in profile")
    if not history:
        return _missing("professional.work_history")
    worked = bool(company) and any(company in h or h in company for h in history)
    return _yn(worked, 0.8)


def h_skill_yes_no(ctx, p, q):
    skills = _question_skills(q)
    if not skills or not (_wants_yes_no(ctx) or ctx.field_type in {"select", "radio"}):
        return None
    mine = {canonicalize(s) for s in p.skills.all}
    has = all(canonicalize(s) in mine for s in skills)
    return _yn(has, 0.88 if has else 0.7, note=f"skills: {', '.join(skills)}")


def h_about(ctx, p, q):
    if p.professional.summary and "YOUR_" not in p.professional.summary:
        text = p.professional.summary
        if ctx.max_length and len(text) > ctx.max_length:
            text = text[: ctx.max_length].rsplit(" ", 1)[0]
        return Answer(text, 0.92, "profile", note="profile summary")
    return OPEN_ENDED


def h_open(ctx, p, q):
    if re.search(r"additional\s+information|anything\s+else|other\s+comments|message\s+to", q) and not ctx.required:
        return Answer("", 0.9, "derived", note="optional free-text left blank")
    return OPEN_ENDED


# (pattern, handler, label_only): label_only rules apply to short noun-phrase labels
# like "Phone number" - never to longer questions such as "Years of mobile development?"
RULES: list[tuple[str, Handler, bool]] = [
    (r"country\s+code|phone\s+code|dial(?:l?ing)?\s+code|calling\s+code", h_country_code, False),
    (r"\bfirst\s*name\b|\bgiven\s+name\b|\bfname\b", _personal("first_name", "first"), True),
    (r"\blast\s*name\b|\bsurname\b|\bfamily\s+name\b|\blname\b", _personal("last_name", "last"), True),
    (r"^(?:your\s+)?(?:full\s+|legal\s+|candidate'?s?\s+)?name$|^full\s+name|^legal\s+name", h_full_name, True),
    (r"\be-?mail\b", _personal("email", "email"), True),
    (r"\bphone\b|\bmobile\b|contact\s+(?:number|no)|\bcell\b|whatsapp", h_phone, True),
    (r"linked\s*in", _personal("linkedin_url", "linkedin"), True),
    (r"git\s*hub", _personal("github_url", "github"), True),
    (r"portfolio|personal\s+(?:website|site|url)|^website|blog\b", _personal("portfolio_url", "portfolio"), True),
    (r"postal|zip\s*code|\bzip\b|pin\s*code|pincode", _personal("postal_code", "postal"), True),
    (r"\bstate\b|province", _personal("state", "state"), True),
    (r"^country$|country\s+of\s+residence|which\s+country|^country\s", _personal("country", "country"), True),
    (r"street|address\s+line|^address|residential\s+address", _personal("address", "address"), True),
    (r"current\s+(?:ctc|salary|compensation|package|annual\s+(?:ctc|salary|compensation))|present\s+(?:ctc|salary)|\bcctc\b|current\s+fixed", h_salary("current"), False),
    (r"expected\s+(?:ctc|salary|compensation|package|annual)|salary\s+expectation|desired\s+(?:salary|compensation|pay)|\bectc\b|compensation\s+expectation|expected\s+fixed", h_salary("expected"), False),
    (r"salary\s+currency|currency", h_salary_currency, True),
    (r"notice\s+period|how\s+soon\s+can\s+you\s+join|joining\s+(?:time|date)|earliest\s+(?:start|joining|possible)|when\s+can\s+you\s+(?:start|join)|available\s+to\s+(?:start|join)|join\s+immediately|start\s+date|serving\s+notice", h_notice, False),
    (r"experience\s+level|seniority\s+level|career\s+level", h_experience_level, False),
    (r"how\s+many\s+(?:years|months)|years?\s+of\s+(?:\w+\s+){0,3}experience|(?:total|overall|relevant|work|professional)\s+experience|experience\s+in\s+(?:years|months)|\byoe\b|years\s+have\s+you", h_experience, False),
    (r"relocat", h_relocate, False),
    (r"preferred\s+(?:work\s+|job\s+)?locations?", h_preferred_location, False),
    (r"^(?:current\s+)?city\b|current\s+(?:city|location)|^location$|^location\s|where\s+are\s+you\s+(?:currently\s+)?(?:based|located|living)|place\s+of\s+residence|city\s+of\s+residence", h_city, False),
    (r"(?:work|working)\s+from\s+(?:the\s+|our\s+)?office|on[\s-]?site|in[\s-]office|in[\s-]person|come\s+to\s+(?:the\s+|our\s+)?office|\bwfo\b|hybrid|commut|work\s+remotely|remote\s+work", h_onsite, False),
    (r"sponsor", h_sponsorship, False),
    (r"authori[sz]ed\s+to\s+work|legally\s+(?:authori[sz]ed|eligible|permitted)|right\s+to\s+work|work\s+permit|eligible\s+to\s+work|work\s+authori[sz]ation", h_authorization, False),
    (r"(?:at\s+least|over|above|older\s+than)\s+18|18\s+years\s+(?:of\s+age|or\s+older|old)|legal\s+(?:working\s+)?age", h_adult, False),
    (r"graduat\w*\s+year|year\s+of\s+(?:graduation|passing|completion)|passing\s+year|passed\s+out", h_grad_year, False),
    (r"\bgpa\b|\bcgpa\b|percentage|\bgrade\b|\bmarks\b", h_grade, True),
    (r"field\s+of\s+study|\bmajor\b|speciali[sz]ation|discipline|\bstream\b", h_field_of_study, True),
    (r"(?:university|college|school|institut\w*)(?:\s+name)?$|name\s+of\s+(?:the\s+)?(?:university|college|institut)|^school|^university|^college", h_institution, True),
    (r"highest\s+(?:level\s+of\s+)?(?:education|qualification|degree)|\bdegree\b|\bqualification\b|level\s+of\s+education|bachelor'?s|master'?s", h_degree, False),
    (r"current\s+(?:company|employer|organi[sz]ation)|present\s+(?:company|employer|organi[sz]ation)|(?:company|employer)\s+name|most\s+recent\s+(?:company|employer)|current\s+or\s+(?:most\s+)?recent\s+(?:company|employer)", h_current_company, False),
    (r"current\s+(?:job\s+)?(?:title|designation|role|position)|^(?:job\s+)?title$|designation|most\s+recent\s+(?:job\s+)?title|current\s+or\s+(?:most\s+)?recent\s+(?:job\s+)?title", h_current_title, False),
    (r"currently\s+(?:employed|working)", h_employed, False),
    (r"\benglish\b", h_english, False),
    (r"\bgender\b|\bsex\b", _diversity("gender"), False),
    (r"pronoun", _diversity("pronouns"), False),
    (r"veteran|military|armed\s+forces", _diversity("veteran_status"), False),
    (r"disabilit|handicap|differently[\s-]abled", _diversity("disability_status"), False),
    (r"ethnic|\brace\b|racial|hispanic|latino", _diversity("ethnicity"), False),
    (r"how\s+did\s+you\s+(?:hear|find|learn|come\s+across)|where\s+did\s+you\s+(?:hear|find|see)|source\s+of\s+(?:application|referral)|referral\s+source|^source$", h_source, False),
    (r"referr(?:ed|al)\s+by|employee\s+referral|referral\s+(?:name|code)|who\s+referred|were\s+you\s+referred", h_referral, False),
    (r"(?:previously|ever|before)\s+(?:worked|been\s+employed|applied|interviewed)|worked\s+(?:for|at|with)\s+(?:us|this\s+company)\s+before|former\s+employee", h_previous_employment, False),
    (r"privacy|terms\s+(?:and|&)\s+conditions|\bconsent\b|i\s+agree|acknowledge|i\s+certify|information\s+(?:provided\s+)?is\s+(?:true|accurate)", h_consent, False),
    (r"tell\s+(?:us|me)\s+about\s+yourself|about\s+yourself|describe\s+yourself|introduce\s+yourself|professional\s+summary|^summary$", h_about, False),
    (r"why\s+(?:are\s+you|do\s+you|would\s+you|should\s+we|this|our|join|us)|interest(?:ed)?\s+in|motivat|cover\s+letter|what\s+makes\s+you|additional\s+information|anything\s+else|message\s+to|describe\s+(?:a|your|an)|tell\s+us\s+(?:about|why)|explain|elaborate", h_open, False),
    (r"(?:do\s+you\s+have|have\s+you|are\s+you)\s+(?:any\s+)?(?:\w+\s+)?(?:experience|exposure|worked|proficien|familiar|comfortable|skilled|knowledge|expert)|\bproficien|knowledge\s+of|familiar\s+with|hands[\s-]on", h_skill_yes_no, False),
]

_COMPILED = [(re.compile(rx, re.I), h, label_only) for rx, h, label_only in RULES]
_LABEL_PREFIX = re.compile(r"^(?:(?:what\s+is|what's|please\s+(?:enter|provide|share|mention)|enter|provide|share|mention)\s+(?:your\s+)?|your\s+)")
_QUESTION_START = re.compile(r"^(?:do|does|are|is|have|has|would|will|can|could|how|what|why|which|did|were|please|describe|tell)\b")


def resolve(ctx: QuestionContext, profile: Profile) -> Answer | None:
    q = normalize_question(ctx.question)
    if not q:
        return None
    label_q = _LABEL_PREFIX.sub("", q)
    is_label = len(label_q.split()) <= 8 and not _QUESTION_START.search(label_q)
    for rx, handler, label_only in _COMPILED:
        if label_only and not is_label:
            continue
        if rx.search(label_q if label_only else q):
            ans = handler(ctx, profile, q)
            if ans is not None:
                if ans is not OPEN_ENDED:
                    ans.note = ans.note or f"rule: {handler.__name__}"
                return ans
    # Fallback: a bare skill mention in a numeric/years field ("React (years)")
    if ctx.field_type == "number" and _question_skills(q):
        return h_experience(ctx, profile, q)
    return None
