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

from app.answers.options import choose_decline, real_options
from app.matching.analyzer import JobInput, normalize_city, parse_locations, seniority_for_years
from app.matching.skills import canonicalize
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
    page_context: str = ""  # visible text of the application page (company context for written answers)
    section: dict[str, str] = field(default_factory=dict)  # values of the group this field is in, e.g.
    # {"company": "Bag Convergence", "title": "Software Engineer"} for one entry of a work-experience list


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
    category: str = ""  # question intent (see answers/intent.py)
    mode: str = ""  # profile | derived | ai_generated | composed | memory | ai_derived | custom | user | ask | skipped
    sources: list[str] = field(default_factory=list)  # profile keys the answer is grounded in
    verified: bool | None = None  # set after filling: the form control shows the value and no error
    rule: str = ""  # the fact/derivation rule that produced it (for source attribution)
    choices: list[str] = field(default_factory=list)  # a multi-select answer: the exact option texts to tick


OPEN_ENDED = Answer(None, 0.0, "open_ended", open_ended=True)

PORTAL_NAMES = {"naukri": "Naukri.com", "linkedin": "LinkedIn", "instahyre": "Instahyre", "mock": "Job portal"}


def normalize_question(q: str) -> str:
    q = re.sub(r"\(\s*(?:required|optional|mandatory)\s*\)", " ", q or "", flags=re.I)
    q = re.sub(r"[*:?]+", " ", q)
    return re.sub(r"\s+", " ", q).strip().lower()


# words that forms glue together in a field name used as its label ('mobilephone', 'currentctc', 'dateofbirth')
_GLUED = frozenset("mobile phone tele cell first last middle full name email address current expected notice period "
                   "company employer job title designation date birth postal zip pin code city state country linkedin "
                   "github portfolio website url number contact total relevant experience salary ctc of location years "
                   "profile".split())
# misspellings seen in recruiters' own questions ("Immedaite Joiner only", "year of experince")
_TYPOS = {"immedaite": "immediate", "immidiate": "immediate", "imediate": "immediate", "immediatly": "immediately",
          "immediatley": "immediately", "experince": "experience", "expereince": "experience", "experiance": "experience",
          "exprience": "experience", "noitce": "notice", "salery": "salary", "sallary": "salary", "currrent": "current",
          "relevent": "relevant", "relocte": "relocate", "avaliable": "available", "availabe": "available"}
_TYPO_RX = re.compile(r"\b(?:" + "|".join(_TYPOS) + r")\b")
_DOTTED_ABBREVIATION = re.compile(r"(?<![a-z.])((?:[a-z]\.){2,}[a-z])\.?(?![a-z])")
# "exp" as recruiters write it (LinkedIn, run #45: "What's your total IT Exp", "How much exp do you have in
# Typescript"): experience - except before a pay / joining word, where it is "expected" ("Exp CTC", "Exp. salary").
# "exp date" is left alone: it may be an expiry date.
_EXP_EXPECTED = re.compile(r"\bexp\b\.?(?=\s+(?:ctc|salary|pay|package|compensation|lpa|doj|joining|hike|remuneration|"
                           r"hourly|monthly|annual|rate)\b)")
_EXP = re.compile(r"\bexp\b\.?(?!\s*(?:date|dt)\b)")
_REL = re.compile(r"\brel\b\.?(?=\s+experience\b)")


def _unglue(word: str) -> str:
    """'mobilephone' -> 'mobile phone' when the whole word is made of known field-name words (else unchanged)."""
    if word in _GLUED or len(word) < 6:
        return word
    best: list[list[str] | None] = [None] * (len(word) + 1)
    best[0] = []
    for end in range(1, len(word) + 1):
        for start in range(end):
            head = best[start]
            if head is not None and word[start:end] in _GLUED and (best[end] is None or len(head) + 1 < len(best[end])):
                best[end] = [*head, word[start:end]]
    parts = best[len(word)]
    return " ".join(parts) if parts and len(parts) >= 2 else word


def repair_words(text: str) -> str:
    """Lower-case question text with the spellings forms use for ordinary labels made readable: 'notice - period',
    'mobilephone', 'current_ctc', 'd.o.b' -> 'notice period', 'mobile phone', 'current ctc', 'dob'. Punctuation that
    carries meaning ('?') is kept; the saved-answer key (normalize_question) is not affected."""
    t = (text or "").lower().replace("_", " ")
    t = _TYPO_RX.sub(lambda m: _TYPOS[m.group(0)], t)
    t = re.sub(r"(?<=[a-z])\s+[-\u2013\u2014]\s+(?=[a-z])", " ", t)
    t = _DOTTED_ABBREVIATION.sub(lambda m: m.group(1).replace(".", ""), t)
    t = _REL.sub("relevant", _EXP.sub("experience", _EXP_EXPECTED.sub("expected", t)))
    if len(t.split()) <= 4:  # a field name, never a sentence
        t = re.sub(r"[a-z]{6,}", lambda m: _unglue(m.group(0)), t)
    return re.sub(r"[ \t]+", " ", t).strip()


def repair_label(question: str) -> str:
    """The question as the rules read it (normalised, then repaired - see repair_words)."""
    return repair_words(normalize_question(question))


def fmt_num(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:.1f}".rstrip("0").rstrip(".")


def _yn(value: bool, conf: float, note: str = "") -> Answer:
    return Answer("Yes" if value else "No", conf, "profile", yes_no=value, note=note)


def _missing(fact: str) -> Answer:
    return Answer(None, 0.0, "profile", needs_review=True, note=f"profile.{fact} is not set")


def _is_numeric_field(ctx: QuestionContext) -> bool:
    return ctx.field_type == "number"


_YES_NO_QUESTION = re.compile(r"^\s*(?:do|does|did|have|has|are|is|can|will|would)\s+(?:you|your)\b", re.I)


def _wants_yes_no(ctx: QuestionContext) -> bool:
    if ctx.field_type == "checkbox":
        return True
    opts = {o.strip().lower() for o in ctx.options}
    if not opts and ctx.field_type in {"select", "radio", "combobox"}:  # a dropdown whose choices could not be read:
        return bool(_YES_NO_QUESTION.search(ctx.question or ""))     # "Do you have ... experience?" is yes / no
    return bool(opts) and ("yes" in opts or "no" in opts)


# ------------------------------------------------------------ handlers ---

Handler = Callable[[QuestionContext, Profile, str], "Answer | None"]


def _personal(attr: str, label: str) -> Handler:
    def h(ctx: QuestionContext, p: Profile, q: str) -> Answer | None:
        v = getattr(p.personal, attr)
        return Answer(v, 0.99, "profile") if v and "YOUR_" not in v else _missing(f"personal.{attr}")
    h.__name__ = f"h_{label}"
    return h


def h_portfolio(ctx, p, q):
    """'Portfolio' / 'Resume / Portfolio link': your portfolio site, else your GitHub profile (a developer's
    portfolio; the same fallback as "links to your work")."""
    site = p.personal.portfolio_url
    if site and "YOUR_" not in site:
        return Answer(site, 0.99, "profile", sources=["personal.portfolio_url"])
    code = p.personal.github_url
    if code and "YOUR_" not in code and not re.search(r"personal\s+(?:website|site)|^website|\bblog\b", q):
        return Answer(code, 0.86, "profile", sources=["personal.github_url"],
                      note="your GitHub profile (no portfolio site in your profile)")
    return _missing("personal.portfolio_url")


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


_ANYWHERE = re.compile(r"pan[\s-]?india|anywhere|any\s+(?:location|city)|all\s+(?:locations|cities|over\s+india)|open\s+to\s+all", re.I)


def h_preferred_location(ctx, p, q):
    locs = p.work_preferences.preferred_locations
    if not locs:
        return _missing("work_preferences.preferred_locations")
    opts = real_options(ctx.options)
    if opts:
        # a list to choose from: your cities that are on it; "Pan India" / "Anywhere" when you are willing to relocate
        mine = {n for x in [*locs, p.personal.current_city] if (n := normalize_city(x or ""))}

        def is_mine(option: str) -> bool:
            if any(x.lower() == option.lower() for x in locs):
                return True
            place = option.split(",")[0].split("(")[0].strip()  # "Bengaluru, Karnataka" / "Bengaluru (Bangalore)"
            city = normalize_city(place)
            # ("Bengaluru Rural" and "Navi Mumbai" are other places, although they read like yours)
            return bool(city) and city in mine and len(place.split()) <= len(city.split())

        hits = [o for o in opts if is_mine(o)]
        if hits:
            return Answer("; ".join(hits[:5]), 0.93, "profile", choices=hits[:5], note="your preferred locations on this list")
        anywhere = next((o for o in opts if _ANYWHERE.search(o)), None)
        if anywhere and p.work_preferences.willing_to_relocate:
            return Answer(anywhere, 0.88, "derived", note="none of your cities is listed; you are willing to relocate",
                          sources=["work_preferences.willing_to_relocate"])
        return Answer(None, 0.0, "profile", needs_review=True, note="none of your preferred locations is offered")
    return Answer(", ".join(locs), 0.93, "profile")


def h_salary(kind: str) -> Handler:
    def h(ctx: QuestionContext, p: Profile, q: str) -> Answer | None:
        lpa = p.professional.current_ctc_lpa if kind == "current" else p.professional.expected_ctc_lpa
        if lpa is None:
            return _missing(f"professional.{kind}_ctc_lpa")
        if foreign := re.search(r"\busd\b|\bus\s*\$|\$|dollars?|\beuros?\b|\beur\b|\bgbp\b|\bpounds?\b|\baed\b|\bsgd\b", q):
            return Answer(None, 0.0, "profile", needs_review=True,
                          note=f"your {kind} CTC is recorded in INR; this question asks for {foreign.group(0).upper()} "
                               "(never converted for you)")
        if ctx.options:
            # option ranges are usually in lakhs ("10-15 LPA"); rupee amounts ("10,00,000") are rescaled
            sample = re.sub(r"(?<=\d),(?=\d)", "", " ".join(ctx.options))
            scale = 1 / 100_000 if re.search(r"\d{6,}", sample) else 1.0
            return Answer(f"{fmt_num(lpa)} LPA", 0.9, "profile", numeric=lpa, unit_scale=scale)
        abroad = re.search(r"united\s+states|\busa?\b|united\s+kingdom|\buk\b|canada|germany|europe|singapore|dubai|"
                           r"\buae\b|australia|netherlands|ireland", (ctx.job.location if ctx.job else "") or "", re.I)
        if abroad and not re.search(r"lakh|lac|\blpa\b|\binr\b|rupees|₹", q):
            return Answer(None, 0.0, "profile", needs_review=True,
                          note=f"your {kind} CTC is in INR; this job is outside India ({ctx.job.location})")
        if re.search(r"lakh|lac|\blpa\b|\bin\s+l\b|lakhs", q):
            return Answer(fmt_num(lpa), 0.97, "profile", numeric=lpa)
        if re.search(r"per\s+month|monthly", q):
            return Answer(str(int(round(lpa * 100_000 / 12))), 0.92, "profile")
        if ctx.field_type in {"textarea", "contenteditable"}:  # a written answer: say it the readable way
            return Answer(f"{fmt_num(lpa)} LPA (INR {int(round(lpa * 100_000)):,} per annum)", 0.93, "profile", numeric=lpa)
        if re.search(r"\binr\b|rupees|₹|\brs\.?\b|annual|per\s+annum|\bp\.?a\.?\b|absolute|in\s+numbers", q):
            return Answer(str(int(round(lpa * 100_000))), 0.92, "profile")
        if _is_numeric_field(ctx):  # a bare number box: annual rupees cannot be misread (a box whose max rules
            # this out is refused by the numeric fitter and asked)
            return Answer(str(int(round(lpa * 100_000))), 0.88, "profile", numeric=lpa,
                          note=f"{fmt_num(lpa)} LPA entered as annual INR (the box states no unit)")
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
    lwd = parse_profile_date(p.professional.last_working_day)
    if ctx.field_type == "date" or re.search(r"\bdate\b", q):
        def shown(d: date) -> str:  # 'Date available (DD/MM/YYYY)' -> that format; a date control takes ISO
            return (format_date_for(q, d) if ctx.field_type != "date" else None) or d.isoformat()

        if p.professional.serving_notice and lwd and lwd >= date.today():
            return Answer(shown(lwd + timedelta(days=1)), 0.9, "derived", note="day after your last working day")
        start = date.today() + timedelta(days=days)
        return Answer(shown(start), 0.88, "derived", note=f"today + {days} days notice")
    if (re.search(r"how\s+soon|when\s+can\s+you|earliest|available\s+to\s+(?:start|join)", q) and not ctx.options
            and not _wants_yes_no(ctx) and not _is_numeric_field(ctx)):
        if p.professional.serving_notice and lwd and lwd >= date.today():
            return Answer(f"After my last working day ({p.professional.last_working_day})", 0.93, "derived",
                          note="serving notice")
        if days == 0:
            return Answer("Immediately", 0.95, "derived", note="notice period 0 days")
        return Answer(f"Within {days} days", 0.93, "derived", note=f"notice period {days} days")
    if re.search(r"immediate\s+join", q) and not ctx.options and not _is_numeric_field(ctx):
        # "Immediate joiner only" typed as a statement: can you?
        if days <= 15:
            return Answer("Yes", 0.9, "derived", yes_no=True, note=f"notice period {days} days")
        return Answer(f"No - my notice period is {days} days", 0.9, "derived", yes_no=False, note=f"notice period {days} days")
    if ctx.options and not (_wants_yes_no(ctx) and re.search(r"immediate|serving", q)):  # (Yes / No chips: below)
        opts_low = " ".join(ctx.options).lower()
        scale = 30.0 if "month" in opts_low and "day" not in opts_low else 1.0
        picked = _choose_notice_option(days, ctx.options)
        if picked:
            return Answer(picked, 0.95, "profile")
        return Answer(fmt_num(days / scale), 0.6, "profile", needs_review=True, note="no matching notice option")
    if _is_numeric_field(ctx) or re.search(r"\bin\s+(?:days|months)\b|number\s+of\s+days|how\s+many\s+(?:days|months|weeks)", q):
        if re.search(r"month", q):
            return Answer(fmt_num(round(days / 30, 1)), 0.95, "profile")
        if re.search(r"how\s+many\s+weeks", q):
            return Answer(fmt_num(round(days / 7, 1)), 0.95, "profile")
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


def parse_profile_date(text: str) -> date | None:
    """'5 March 2026', '2026-03-05', '05/03/2026' (day first) -> date."""
    from datetime import datetime

    t = (text or "").strip()
    if not t:
        return None
    for fmt in ("%Y-%m-%d", "%d %B %Y", "%d %b %Y", "%B %d %Y", "%B %d, %Y", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y",
                "%b %d, %Y"):
        try:
            return datetime.strptime(re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", t), fmt).date()
        except ValueError:
            continue
    return None


_DATE_TOKENS = {"dd": "%d", "mm": "%m", "yyyy": "%Y", "yy": "%y", "mmm": "%b", "mon": "%b"}


def format_date_for(q: str, d: date) -> str | None:
    """The date in the format the label asks for: 'Last Working Date (DD/MM/YY)' -> 05/03/26."""
    m = re.search(r"\b(dd|mm|mmm|mon|yyyy|yy)([/\-. ])(dd|mm|mmm|mon)\2(yyyy|yy|dd)\b", q, re.I)
    if not m:
        return None
    a, sep, b, c = m.group(1).lower(), m.group(2), m.group(3).lower(), m.group(4).lower()
    return d.strftime(sep.join(_DATE_TOKENS[t] for t in (a, b, c)))


def h_last_working_day(ctx, p, q):
    lwd = p.professional.last_working_day
    if not lwd:
        return _missing("professional.last_working_day")
    d = parse_profile_date(lwd)
    if ctx.field_type == "date" and d:
        return Answer(d.isoformat(), 0.97, "profile", note="last working day")
    if d and (shaped := format_date_for(q, d)):
        return Answer(shaped, 0.97, "profile", note=f"last working day {lwd}")
    return Answer(lwd, 0.97, "profile", note="last working day")


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
    from app.answers.intent import question_skills

    return question_skills(q)


_VERB = r"(?:design(?:ing)?|build(?:ing)?|develop(?:ing)?|work(?:ing)?|us(?:e|ing)|implement(?:ing)?|manag(?:e|ing)|maintain(?:ing)?)"


def skill_groups(q: str) -> list[tuple[list[str], bool]]:
    """Skill requirements of a years question: groups joined by 'and' must all hold; inside a group '/', 'or',
    'such as', 'e.g.' list alternatives. 'React/Angular and Node.js/Python' -> [([React, Angular], any), ...]."""
    text = re.sub(rf"\b({_VERB})\s+or\s+{_VERB}\b", r"\1", q)  # "designing or working with" is not a choice
    out: list[tuple[list[str], bool]] = []
    for seg in re.split(r",?\s+\band\b\s+|\s*&\s*", text):
        skills = _question_skills(seg)
        if skills:
            any_of = len(skills) > 1 and bool(re.search(r"/|\bor\b|such\s+as|e\.?\s?g\.?|\blike\b|\bany\b", seg))
            out.append((skills, any_of))
    return out


def _skill_value(p: Profile, s: str) -> tuple[float | None, bool, list[str]]:
    """(years or None, listed in your skills, work-history roles the years were derived from)."""
    canon = canonicalize(s)
    listed = canon in {canonicalize(x) for x in p.skills.all}
    yrs = p.skills.years_for(canon)
    if yrs is None:
        yrs = next((v for k, v in p.skills.years.items() if canonicalize(k) == canon), None)
    derived_from: list[str] = []
    if yrs is None:
        from app.answers.knowledge import CandidateKnowledge, floor_half

        dy, derived_from = CandidateKnowledge(p).skill_years(canon)
        yrs = floor_half(dy) if dy is not None and dy >= 0.5 else None
    return yrs, listed, derived_from


def h_experience(ctx, p, q):
    total = p.professional.total_experience_years
    months = bool(re.search(r"\bmonths?\b", q)) and not re.search(r"years?\s+and\s+months", q)
    skills = _question_skills(q)
    yes_no = _wants_yes_no(ctx)
    overall = re.search(r"(?:total|overall)\s+(?:work\s+|professional\s+|it\s+|industry\s+)?(?:years\s+of\s+)?experience"
                        r"(?!\s+(?:in|with|on|using|of|working\s+with)\b)", q)
    if skills and not overall:  # "Total React experience" is React; "Total experience" is overall
        group_values: list[float] = []
        derived_notes: list[str] = []
        for group, any_of in skill_groups(q) or [(skills, False)]:
            known: list[float] = []
            unknown: Answer | None = None
            for s in group:
                yrs, listed, derived_from = _skill_value(p, s)
                canon = canonicalize(s)
                if yrs is not None:
                    known.append(yrs)
                    if derived_from:
                        derived_notes.append(f"{canon}: {fmt_num(yrs)} yrs from work-history dates")
                elif yes_no and not (listed and re.search(r"\d+(?:\.\d+)?\s*\+?\s*(?:years?|yrs?)", q)):
                    known.append(1.0 if listed else 0.0)  # (a listed skill without years cannot pass a threshold)
                elif not listed and p.skills.complete is True:
                    unknown = unknown or Answer("0", 0.9, "derived", numeric=0.0,
                                                note=f"'{canon}' is not in your skills, which you declared complete")
                elif not listed:
                    unknown = unknown or Answer("0", 0.7, "profile", needs_review=True, numeric=0.0,
                                                note=f"'{canon}' is not in your skills; confirm 0 years (or set "
                                                     "skills.complete to true to have this answered for you)")
                else:
                    unknown = unknown or _missing(f"skills.years['{canon}']")
                if unknown is not None and not any_of:
                    return unknown  # every skill of an "and" list is needed
            if not known:
                return unknown
            group_values.append(max(known) if any_of else min(known))
        yrs = min(group_values)
        if yes_no:
            m = re.search(r"(\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?)", q)
            if m:  # "a minimum of 4 years experience in React?" -> compare, do not just check > 0
                return _yn(yrs >= float(m.group(1)), 0.92, note=f"{', '.join(skills)}: {fmt_num(yrs)} years")
            return _yn(yrs > 0, 0.9, note=f"skills: {', '.join(skills)}")
        v = yrs * 12 if months else yrs
        if derived_notes:
            return Answer(fmt_num(v), 0.9, "derived", numeric=yrs, note="; ".join(derived_notes))
        return Answer(fmt_num(v), 0.95, "profile", numeric=yrs, note=f"skill years for {', '.join(skills)}")
    if re.search(r"relevant|front[\s-]?end|ui\s+development|web\s+development", q):
        yrs = p.professional.relevant_experience_years or None
        if yrs is None:
            return _missing("professional.relevant_experience_years")
    elif not overall and (topic := _WITH_TOPIC.search(q)) and not _GENERAL_TOPIC.search(topic.group(1)):
        # LinkedIn's "How many years of work experience do you have with Databases?": a named skill / tool that is
        # not among your skills - the total would claim all your years for it
        return Answer(None, 0.0, "profile", needs_review=True,
                      note=f"'{topic.group(1).strip()[:40]}' is not in your skills (your total would overstate it)")
    elif not overall and (other := _OTHER_DISCIPLINE.search(q)):
        # "years in full-stack development / UI design / team leadership": your total years would overstate a
        # discipline the profile does not record - asked once, then remembered
        return Answer(None, 0.0, "profile", needs_review=True,
                      note=f"years of {other.group(0).strip()} experience are not in your profile (your total would overstate them)")
    else:
        yrs = total
    if yrs is None:
        return _missing("professional.total_experience_years")
    if re.search(r"whole\s+years", q):  # the years box of a years + months pair
        return Answer(str(int(yrs)), 0.97, "profile", numeric=float(int(yrs)))
    if yes_no:
        m = re.search(r"(\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?)", q)
        if m:
            return _yn(yrs >= float(m.group(1)), 0.95)
        return _yn(yrs > 0, 0.85)
    v = yrs * 12 if months else yrs
    return Answer(fmt_num(v), 0.99, "profile", numeric=yrs)


_WITH_TOPIC = re.compile(r"years\s+of\s+(?:work\s+|professional\s+|hands[\s-]?on\s+)?experience\s+do\s+you\s+have\s+"
                         r"(?:with|in|using|on)\s+(.+?)\s*\??$", re.I)
_GENERAL_TOPIC = re.compile(r"\b(?:software|programming|coding|engineering|it|information\s+technology|technology|web|"
                            r"front[\s-]?end|ui\s+development|development|the\s+industry|this\s+(?:role|field|domain))\b", re.I)
_OTHER_DISCIPLINE = re.compile(
    r"full[\s-]?stack|back[\s-]?end|server[\s-]?side|\bmobile\b|\bandroid\b|\bios\b|devops|\bcloud\b|data\s+(?:science|"
    r"engineering|analy\w*)|machine\s+learning|user\s+interface\s+design|\bui\s*/\s*ux\b|\bux\b|(?:graphic|product|"
    r"visual|interaction)\s+design|team\s+lead\w*|leading\s+(?:a\s+)?teams?|leadership|people\s+management|"
    r"(?<!state\s)management|managing\s+(?:a\s+)?teams?|\btesting\b|\bqa\b|embedded|\bsales\b|teaching", re.I)


def h_how_long(ctx, p, q):
    """'How long have you used React in production?': the years of the skill it names (a question without a skill
    is not about your experience - 'How long have you been looking for a job?')."""
    if not _question_skills(q) or ctx.field_type in {"textarea", "contenteditable"}:
        return None
    return h_experience(ctx, p, q)


def h_months_remainder(ctx, p, q):
    """'Total additional months of experience' (next to a years box): the months part of your total experience."""
    total = p.professional.total_experience_years
    if total is None:
        return _missing("professional.total_experience_years")
    months = int(round((total - int(total)) * 12)) % 12
    return Answer(str(months), 0.95, "derived", numeric=float(months),
                  note=f"months part of {fmt_num(total)} years of experience")


def h_fresher(ctx, p, q):
    yrs = p.professional.total_experience_years
    if yrs is None:
        return _missing("professional.total_experience_years")
    note = f"{fmt_num(yrs)} years of experience"
    opts = real_options(ctx.options)
    if opts and not _wants_yes_no(ctx):
        want = r"experienced|professional|working" if yrs >= 1 else r"fresher|graduate|entry"
        pick = next((o for o in opts if re.search(want, o, re.I)), None)
        return Answer(pick, 0.95, "derived", note=note) if pick else None
    return _yn(yrs < 1, 0.95, note=note)


_LOCATED_IN = re.compile(r"(?:located|based|living|residing|staying|currently)\s+(?:in|at|out\s+of|near)\s+"
                         r"([a-z][a-z .\-]{1,40}?)(?:\s+(?:currently|now|right\s+now|city|area|region))?\s*(?:\?|$|,)")


def h_located_in(ctx, p, q):
    """'Are you located in Bangalore?' -> from your current city (relocation questions are handled separately)."""
    m = _LOCATED_IN.search(q)
    if not m:
        return None
    mine = normalize_city(p.personal.current_city)
    if not mine:
        return _missing("personal.current_city")
    place = m.group(1).strip()
    asked = normalize_city(place)
    if asked:
        return _yn(asked == mine, 0.93, note=f"you live in {p.personal.current_city}")
    if place in {(p.personal.country or "").lower(), (p.personal.state or "").lower()} - {""}:
        return _yn(True, 0.93, note=f"you live in {p.personal.current_city}, {p.personal.country}")
    return None


def _norm_company(name: str) -> str:
    n = re.sub(r"\b(?:pvt|private|ltd|limited|inc|llp|llc|corp|corporation|co|technologies|technology|solutions|"
               r"software|services|india)\b\.?", "", (name or "").lower())
    return re.sub(r"[^a-z0-9]", "", n)


def section_entry(ctx: QuestionContext, p: Profile):
    """The work-history entry of the group this field belongs to (by its Company field), if any."""
    co = _norm_company((ctx.section or {}).get("company", ""))
    if not co:
        return None
    for w in p.professional.work_history:
        wc = _norm_company(w.company)
        if wc and (wc == co or (min(len(wc), len(co)) >= 4 and (wc in co or co in wc))):
            return w
    return None


def h_role_description(ctx, p, q):
    """'Description' inside one work-experience entry: that role's summary from your work history (verbatim)."""
    if not (ctx.section or {}).get("company"):
        return None
    w = section_entry(ctx, p)
    if w is None:
        return Answer(None, 0.0, "profile", needs_review=True,
                      note=f"'{ctx.section.get('company')}' is not in your work history")
    text = (w.summary or "").strip()
    techs = [t for t in (w.technologies or []) if t]
    if techs:
        text = (text.rstrip(".") + ". " if text else "") + "Technologies: " + ", ".join(techs) + "."
    if not text:
        return _missing(f"professional.work_history[{w.company}].summary")
    return Answer(text, 0.95, "profile", note=f"your work history: {w.title} at {w.company}",
                  sources=[f"work_history: {w.company}"])


def h_work_links(ctx, p, q):
    """'Please share links to your recent frontend work' -> your portfolio / GitHub (never made up)."""
    links = [x for x in (p.personal.portfolio_url, p.personal.github_url) if x and "YOUR_" not in x]
    if not links:
        return _missing("personal.portfolio_url")
    return Answer("\n".join(links) if ctx.field_type in {"textarea", "contenteditable"} else links[0], 0.93, "profile",
                  note="your portfolio / GitHub", sources=["personal.portfolio_url", "personal.github_url"])


def h_experience_threshold(ctx, p, q):
    """'Only 8+ years apply. Less experience don't apply?' -> Yes / No against your experience (never stretched)."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?)", q)
    if not m:
        return None
    skills = _question_skills(q)
    yrs = None
    if skills:
        yrs, _listed, _ = _skill_value(p, skills[0])
    if yrs is None:
        yrs = p.professional.total_experience_years
    if yrs is None:
        return _missing("professional.total_experience_years")
    return _yn(yrs >= float(m.group(1)), 0.93, note=f"{fmt_num(yrs)} years against the {m.group(1)}+ asked")


def _job_area(ctx) -> str:
    t = f"{ctx.job.title if ctx.job else ''}".lower()
    if re.search(r"full[\s-]?stack", t):
        return "fullstack"
    if re.search(r"\bai\b|machine\s+learning|\bml\b|gen\s?ai|llm", t):
        return "ai"
    return "frontend"


_AREA_OPTION = {"frontend": r"front[\s-]?end|\bui\b|web\s+(?:dev|development)|react",
                "fullstack": r"full[\s-]?stack", "ai": r"\bai\b|machine\s+learning|\bml\b"}


def h_role_category(ctx, p, q):
    """'Select a Category' (AI/ML, Backend, Frontend...): the category of the posting you are applying to."""
    opts = real_options(ctx.options)
    if not opts:
        return None
    area = _job_area(ctx)
    for rx in (_AREA_OPTION[area], _AREA_OPTION["frontend"]):
        pick = next((o for o in opts if re.search(rx, o, re.I)), None)
        if pick:
            return Answer(pick, 0.9, "derived", note=f"the {area} posting you are applying to")
    return None


def h_primary_technology(ctx, p, q):
    """'technology' / 'primary skill' with options: your primary skill that this job asks for (in your order)."""
    opts = real_options(ctx.options)
    if not opts:
        return None
    jd = f"{ctx.job.title} {ctx.job.description}".lower() if ctx.job else ""
    mine = [canonicalize(x) for x in p.skills.primary]
    by_canon = {canonicalize(o): o for o in opts}
    for skill in mine:
        if skill in by_canon and (not jd or skill.lower() in jd):
            return Answer(by_canon[skill], 0.88, "derived", note=f"your primary skill {skill}, asked by this job")
    for skill in mine:
        if skill in by_canon:
            return Answer(by_canon[skill], 0.85, "derived", note=f"your primary skill {skill}")
    # options that are roles ('React JS Developer', 'PHP Developer'): the one naming your primary skill this job asks for
    naming = {o: {canonicalize(x) for x in _question_skills(o)} for o in opts}
    for skill in mine:
        hits = [o for o in opts if skill in naming[o]]
        if len(hits) == 1 and (not jd or skill.lower() in jd):
            return Answer(hits[0], 0.86, "derived", note=f"your primary skill {skill}, asked by this job")
    if ctx.job and ctx.job.title and (hit := match_position(ctx.job.title, opts)):
        return Answer(hit[0], hit[1], "derived", note=f"the job you are applying to ({hit[2]})")
    return None


def h_area_of_interest(ctx, p, q):
    titles = [t for t in p.job_preferences.target_titles if t][:2]
    skills = [s for s in p.skills.primary if s][:3]
    if not titles and not skills:
        return _missing("job_preferences.target_titles")
    text = " / ".join(titles) + (f" ({', '.join(skills)})" if skills else "")
    return Answer(text.strip(" /"), 0.9, "profile", note="your target roles and primary skills",
                  sources=["job_preferences.target_titles", "skills.primary"])


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


_CITY_CHOICE = re.compile(r"(?:select|choose|which|enter|mention)\s+(?:the\s+|your\s+)?(?:\w+\s+){0,2}cit(?:y|ies)|"
                          r"\bcity\s+(?:you\s+are\s+)?(?:currently\s+)?(?:residing|living|based|located)|"
                          r"current\s+or\s+preferred\s+(?:city|location)|(?:residing|based)\s+or\s+willing", re.I)


def h_location_choice(ctx, p, q):
    """'Please select the city you are currently residing or willing to relocate to' -> a city, not Yes/No."""
    city = p.personal.current_city
    prefs = [x for x in p.work_preferences.preferred_locations if x.strip()]
    opts = real_options(ctx.options)
    if opts:
        for cand in [city, *prefs]:
            canon = normalize_city(cand or "")
            for o in opts:
                if cand and (canon and normalize_city(o) == canon or cand.lower() in o.lower()):
                    return Answer(o, 0.93, "derived", note=f"your city / preferred location ({cand})")
        if any(re.search(r"remote", o, re.I) for o in opts) and p.work_preferences.open_to_remote:
            return Answer(next(o for o in opts if re.search(r"remote", o, re.I)), 0.85, "derived",
                          note="you are open to remote work")
        return Answer(None, 0.0, "profile", needs_review=True, note="none of your cities is offered")
    if re.search(r"prefer|would\s+you\s+like\s+to\s+work|want\s+to\s+work", q) and ctx.job and ctx.job.location:
        # "In which city would you prefer to work?" on a job in one of your cities: that city
        mine = {n for x in [city, *prefs] if (n := normalize_city(x or ""))}
        for place in re.split(r"[,/|;()]+", ctx.job.location):
            if (n := normalize_city(place.strip())) and n in mine and n not in {"remote", "anywhere"}:
                return Answer(place.strip(), 0.93, "derived", note="this job's city, one of your preferred locations",
                              sources=["work_preferences.preferred_locations", "job posting"])
    if not city:
        return _missing("personal.current_city")
    return Answer(city, 0.95, "profile", note="current city")


def h_interview_availability(ctx, p, q):
    avail = (p.work_preferences.interview_availability or "").strip()
    if not avail:
        return Answer(None, 0.0, "profile", needs_review=True,
                      note="interview availability is not in your profile (work_preferences.interview_availability)")
    if _wants_yes_no(ctx):
        return _yn(not re.match(r"^\s*(?:no|not)\b", avail, re.I), 0.9, note="interview availability")
    return Answer(avail, 0.92, "profile", note="interview availability")


def h_visa(ctx, p, q):
    """A visa / permit for a country you are not listed as authorized in: only you know (never guessed)."""
    countries = [c.lower() for c in p.work_preferences.authorized_countries]
    mentioned = [c for c in countries if re.search(rf"(?<![a-z]){re.escape(c)}(?![a-z])", q)]
    if mentioned and _wants_yes_no(ctx):
        return _yn(True, 0.9, note=f"you are authorized to work in {mentioned[0].title()}")
    return Answer(None, 0.0, "profile", needs_review=True, note="no verified visa / work-permit information")


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


_COUNTRY_ALIAS = {"usa": "united states", "us": "united states", "u.s.": "united states", "uk": "united kingdom"}
_KNOWN_COUNTRIES = ["india", "united states", "usa", "u.s.", "us", "united kingdom", "uk", "canada", "germany",
                    "singapore", "australia", "uae", "netherlands", "ireland", "european union", "eu"]
_DEMONYM = {"india": "indian", "united states": "american", "united kingdom": "british", "canada": "canadian",
            "germany": "german", "singapore": "singaporean", "australia": "australian", "netherlands": "dutch",
            "ireland": "irish"}
_NEGATED = re.compile(r"\bnot?\b|\bnon[\s-]?|\bwithout\b|\brequir|\bneed|\bsponsor|\bpending\b|\bexpir", re.I)


def _countries_in(q: str) -> list[str]:
    found = [k for k in _KNOWN_COUNTRIES if re.search(rf"(?<![a-z]){re.escape(k)}(?![a-z])", q)]
    return list(dict.fromkeys(_COUNTRY_ALIAS.get(k, k) for k in found))


def _work_country(ctx, q: str, authorized: list[str]) -> str | None:
    """The country a work-authorization question is about: named in it, else the job's (India / remote from India)."""
    named = _countries_in(q)
    if named:
        return named[0]
    job_loc = (ctx.job.location if ctx.job else "") or ""
    if "india" in authorized and (not job_loc or normalize_city(job_loc) or "india" in job_loc.lower()
                                  or "remote" in job_loc.lower()):
        return "india"
    return None


def _status_option(options: list[str], pattern: str) -> str | None:
    """The one option stating a positive status (``Citizen``), never ``Non-citizen`` / ``Requires sponsorship``."""
    hits = [o for o in options if re.search(pattern, o, re.I) and not _NEGATED.search(o)]
    return hits[0] if len(hits) == 1 else None


def h_authorization(ctx, p, q):
    """Work authorization. Yes/No from authorized_countries; a status choice (Citizen / Permanent resident / Work
    visa...) only from what your profile states - being allowed to work somewhere never implies citizenship."""
    wp = p.work_preferences
    authorized = [_COUNTRY_ALIAS.get(c.lower(), c.lower()) for c in wp.authorized_countries]
    if not authorized:
        return _missing("work_preferences.authorized_countries")
    country = _work_country(ctx, q, authorized)
    if country is None:
        return Answer(None, 0.0, "profile", needs_review=True, note="work authorization for this country unknown")
    ok = country in authorized
    options = real_options(ctx.options)
    if not options or _wants_yes_no(ctx):
        note = f"you are authorized to work in {country.title()}" if ok else ""
        if not _countries_in(q) and ok:
            note = f"job is in {country.title()} and you are authorized to work in {country.title()}"
        return _yn(ok, 0.93 if _countries_in(q) else 0.88, note=note)
    citizen_of = {_COUNTRY_ALIAS.get(c.lower(), c.lower()) for c in wp.citizenship}
    if country in citizen_of:
        pick = _status_option(options, r"citizen|national\b|" + re.escape(_DEMONYM.get(country, country)))
        if pick:
            return Answer(pick, 0.93, "profile", note=f"you are a citizen of {country.title()}",
                          sources=["work_preferences.citizenship"])
    if ok and not wp.requires_sponsorship:
        pick = _status_option(options, r"authori[sz]ed|eligible|right\s+to\s+work|permitted|allowed")
        if pick:
            return Answer(pick, 0.9, "profile", note=f"you are authorized to work in {country.title()}")
    shown = ", ".join(options[:6])
    if ok:
        why = (f"the form asks for your status ({shown}); your profile says you may work in {country.title()} but "
               "does not state your citizenship or residency (work_preferences.citizenship)")
    else:
        why = f"the form asks for your work-authorization status for {country.title()} ({shown})"
    return Answer(None, 0.0, "profile", needs_review=True, note=why)


def h_citizenship(ctx, p, q):
    """Citizenship / nationality: only from work_preferences.citizenship (never inferred from where you may work)."""
    citizen_of = [_COUNTRY_ALIAS.get(c.lower(), c.lower()) for c in p.work_preferences.citizenship]
    if not citizen_of:
        return _missing("work_preferences.citizenship")
    asked = _countries_in(q)
    if _wants_yes_no(ctx) or (asked and not ctx.options):
        if asked and asked[0] in citizen_of:
            return _yn(True, 0.95, note=f"you are a citizen of {asked[0].title()}")
        return Answer(None, 0.0, "profile", needs_review=True,
                      note="citizenship of " + (asked[0].title() if asked else "this country") + " is not in your profile")
    options = real_options(ctx.options)
    for c in citizen_of:
        names = [c, _DEMONYM.get(c, c)]
        for o in options:
            if any(re.search(rf"(?<![a-z]){re.escape(n)}(?![a-z])", o.lower()) for n in names):
                return Answer(o, 0.95, "profile", note=f"you are a citizen of {c.title()}")
    if options:
        return Answer(None, 0.0, "profile", needs_review=True, note="none of the options matches your citizenship")
    return Answer(p.work_preferences.citizenship[0], 0.95, "profile")


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


# the education level a question names ("10th Percentage", "12th passing year", "Masters / Bachelors Percentage")
_EDU_LEVEL_Q = [
    ("10th", r"\b10th\b|\bxth\b|\bx\s+(?:std|standard|class|grade)\b|class\s*(?:10|x)\b|\bssc\b|\bsslc\b|matric|"
             r"secondary\s+school\s+(?:certificate|exam)|high\s+school"),
    ("12th", r"\b12th\b|\bxiith?\b|class\s*(?:12|xii)\b|\bhsc\b|intermediate|senior\s+secondary|higher\s+secondary|"
             r"\bpuc\b|pre[-\s]?university"),
    ("bachelor", r"bachelor|\bug\b|under\s?grad|\bb\.?\s?(?:tech|e|sc|com|a)\b|\bbca\b|\bbba\b|graduation\s+(?:percentage|"
                 r"marks|cgpa|gpa|grade)"),
    ("master", r"master|\bpg\b|post[-\s]?grad|\bm\.?\s?(?:tech|e|sc|com|a)\b|\bmca\b|\bmba\b"),
    ("doctorate", r"ph\.?\s?d|doctor"),
]
_EDU_LEVEL_ENTRY = [("doctorate", r"ph\.?\s?d|doctor"),
                    ("master", r"\bm\.?\s?(?:tech|e|sc|s|ca|ba|com)\b|master|\bmca\b|\bmba\b|post\s?grad"),
                    ("bachelor", r"\bb\.?\s?(?:tech|e|sc|s|ca|ba|com|a)\b|bachelor|\bbca\b|\bbba\b|graduat|undergrad"),
                    ("12th", r"12th|\bxii\b|\bhsc\b|intermediate|senior\s+secondary|higher\s+secondary|\bpuc\b"),
                    ("10th", r"10th|\bssc\b|\bsslc\b|matric|high\s+school|secondary")]


def _entry_level(degree: str) -> str:
    return next((lvl for lvl, rx in _EDU_LEVEL_ENTRY if re.search(rx, degree or "", re.I)), "")


def education_for(q: str, p: Profile):
    """(entry, levels asked): the education entry a question is about. A question naming a level you have no
    entry for gets None - your MCA grade is never given as your 10th / 12th percentage."""
    levels = [name for name, rx in _EDU_LEVEL_Q if re.search(rx, q, re.I)]
    if not levels:
        return _highest_education(p), []
    matching = [e for e in p.professional.education if _entry_level(e.degree) in levels]
    if not matching:
        return None, levels
    return max(matching, key=lambda e: (_degree_level(e.degree) or 0, e.end_year or 0)), levels


def h_institution(ctx, p, q):
    ed, levels = education_for(q, p)
    if ed is None and levels:
        return _missing(f"professional.education[{levels[0]}].institution")
    return Answer(ed.institution, 0.95, "profile") if ed and ed.institution else _missing("professional.education[].institution")


def h_grad_year(ctx, p, q):
    ed, levels = education_for(q, p)
    if ed is None and levels:
        return _missing(f"professional.education[{levels[0]}].end_year")
    return Answer(str(ed.end_year), 0.95, "profile", numeric=ed.end_year) if ed and ed.end_year else _missing("professional.education[].end_year")


def h_grade(ctx, p, q):
    ed, levels = education_for(q, p)
    if ed is None and levels:
        return _missing(f"professional.education[{levels[0]}].grade")
    if ed is not None and not ed.grade and not levels:
        graded = [e for e in p.professional.education if e.grade]  # the highest entry that has a grade
        ed = max(graded, key=lambda e: (_degree_level(e.degree) or 0, e.end_year or 0)) if graded else ed
    return Answer(ed.grade, 0.93, "profile", note=f"{ed.degree} grade") if ed and ed.grade else \
        _missing(f"professional.education[{_entry_level(ed.degree) if ed else ''}].grade")


def h_field_of_study(ctx, p, q):
    ed = _highest_education(p)
    return Answer(ed.field, 0.93, "profile") if ed and ed.field else _missing("professional.education[].field")


def h_current_company(ctx, p, q):
    c = p.professional.current_company
    return Answer(c, 0.97, "profile") if c else _missing("professional.current_company")


_ROLE_NOISE = re.compile(r"\b(?:senior|sr|junior|jr|lead|staff|principal|associate|intern(?:ship)?|trainee|fresher|"
                         r"experienced|mid(?:[\s-]level)?|level|i{1,3}|iv|l[1-6]|remote|hybrid|onsite|on[\s-]site|wfh|"
                         r"full[\s-]time|part[\s-]time|contract|permanent|urgent(?:ly)?|hiring|opening|vacancy|"
                         r"\d+(?:\.\d+)?\s*(?:\+|-\s*\d+)?\s*(?:years?|yrs?)(?:\s+(?:of\s+)?exp(?:erience)?)?)\b\.?", re.I)
_TITLE_AREA = [("fullstack", r"full[\s-]?stack|\bmern\b|\bmean\b"),
               ("ai", r"\bai\b|machine\s+learning|\bml\b|gen\s?ai|\bllm\b|data\s+scien"),
               ("frontend", r"front[\s-]?end|\bui\b|\breact|angular|\bvue|web\s+(?:dev|ui)|javascript"),
               ("backend", r"back[\s-]?end|\bpython\b|\bjava\b|\bnode|\.net\b|\bphp\b|golang|django|spring")]


def role_key(title: str) -> str:
    """A job title without seniority, work mode and spelling variants: 'Sr. ReactJS Engineer (Remote)' -> 'react developer'."""
    t = (title or "").lower()
    t = re.sub(r"\breact\s*\.?\s*js\b|\breactjs\b", "react", t)
    t = re.sub(r"\bnode\s*\.?\s*js\b|\bnodejs\b", "node", t)
    t = re.sub(r"front[\s-]?end", "frontend", re.sub(r"back[\s-]?end", "backend", re.sub(r"full[\s-]?stack", "fullstack", t)))
    t = _ROLE_NOISE.sub(" ", t)
    t = re.sub(r"\b(?:engineers?|programmers?|developers|devs?)\b", "developer", t)
    return " ".join(re.sub(r"[^a-z0-9+#. ]", " ", t).replace(" . ", " ").split()).strip(" .")


def _title_area(title: str) -> str:
    return next((area for area, rx in _TITLE_AREA if re.search(rx, title or "", re.I)), "")


def match_position(title: str, options: list[str]) -> tuple[str, float, str] | None:
    """The listed opening that is the job you are applying to: the same title, else the only one that names the
    same technology, else a clearly closest title, else the only one in the same area. None when no option is
    clearly it (a wrong pick would apply you for another job)."""
    from rapidfuzz import fuzz

    opts = real_options(options)
    key = role_key(title)
    if not opts or not key:
        return None
    keys = {o: role_key(o) for o in opts}
    same = [o for o in opts if keys[o] == key]
    if same:
        return same[0], 0.95, "the same title"
    mine = {canonicalize(x) for x in _question_skills(title)}
    pool = opts
    if mine:
        naming = [o for o in opts if mine & {canonicalize(x) for x in _question_skills(o)}]
        if len(naming) == 1:
            return naming[0], 0.9, f"the only opening that names {', '.join(sorted(mine))}"
        pool = naming or opts
    scored = sorted(((fuzz.token_sort_ratio(key, keys[o]), o) for o in pool if keys[o]), reverse=True)
    if scored and scored[0][0] >= 86 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 8):
        return scored[0][1], 0.88, "the closest title"
    area = _title_area(title)
    if area:
        rx = dict(_TITLE_AREA)[area]
        in_area = [o for o in pool if re.search(rx, o, re.I)]
        if len(in_area) == 1:
            return in_area[0], 0.85, f"the only {area} opening"
    return None


def h_position_choice(ctx, p, q):
    """'Select position' / 'Apply for' / 'Job role' with a list of the company's openings: the one you are applying to."""
    opts = real_options(ctx.options)
    title = (ctx.job.title if ctx.job else "") or ""
    if not opts or ctx.field_type not in {"select", "radio", "combobox", "checkbox_group"} or not title:
        return None
    hit = match_position(title, opts)
    if hit is None:
        return Answer(None, 0.0, "derived", needs_review=True,
                      note=f"none of the listed openings clearly matches '{title[:60]}' - please choose")
    return Answer(hit[0], hit[1], "derived", note=f"the job you are applying to ({hit[2]})", sources=["job posting"])


def h_applied_title(ctx, p, q):
    """The position you are applying for: the posting's own title (never your current title)."""
    t = (ctx.job.title if ctx.job else "") or ""
    if not t:
        return None
    if real_options(ctx.options):
        return h_position_choice(ctx, p, q)
    return Answer(t, 0.95, "derived", note="title of the job you are applying for", sources=["job posting"])


def h_subject(ctx, p, q):
    """The 'Subject' line of an application form: what the application is for."""
    t = (ctx.job.title if ctx.job else "") or ""
    if real_options(ctx.options) or ctx.field_type in {"textarea", "contenteditable"}:
        return None
    return Answer(f"Application for {t}" if t else "Job application", 0.9, "derived",
                  note="subject of this application", sources=["job posting"])


def h_current_title(ctx, p, q):
    if (ctx.section or {}).get("company") and not re.search(r"current|present|most\s+recent", q):
        w = section_entry(ctx, p)  # "Title" inside one work-experience entry: that role's title
        if w is None:
            return Answer(None, 0.0, "profile", needs_review=True,
                          note=f"'{ctx.section.get('company')}' is not in your work history")
        return Answer(w.title, 0.95, "profile", note=f"your title at {w.company}") if w.title else \
            _missing(f"professional.work_history[{w.company}].title")
    t = p.professional.current_title
    return Answer(t, 0.97, "profile") if t else _missing("professional.current_title")


def h_employed(ctx, p, q):
    lwd = parse_profile_date(p.professional.last_working_day)
    if lwd and lwd < date.today() and not p.professional.serving_notice:
        return Answer(None, 0.0, "profile", needs_review=True,
                      note=f"your last working day ({p.professional.last_working_day}) has passed but a current company "
                           "is listed - please confirm")
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


_IF_NONE_TYPE = re.compile(r"if\s+(?:no|not|none|not\s+applicable)\b[\s,]*(?:please\s+)?(?:type|enter|write|put|mention|say|"
                           r"state|answer)\s+[\"'\u201c]?(n\s*/\s*a|na|none|nil|no)\b", re.I)
_CONDITIONAL = re.compile(r"^if\s+(?:yes|so|applicable|any|referred)\b|\bif\s+(?:any|applicable)\b", re.I)


def h_referral(ctx, p, q):
    if _wants_yes_no(ctx):
        return _yn(False, 0.85, note="applying directly through a portal")
    if not ctx.required:
        return Answer("", 0.9, "derived", note="no referral")
    if real_options(ctx.options):
        return Answer(None, 0.0, "profile", needs_review=True, note="referral details not in profile")
    if told := _IF_NONE_TYPE.search(ctx.question or ""):  # "(If No, type N/A)": the form says what to enter
        return Answer("N/A" if "/" in told.group(1) else told.group(1), 0.9, "derived",
                      note="no referral (the form says what to enter then)")
    if _CONDITIONAL.search(q):  # "If yes, the name of the employee who referred you"
        return Answer("N/A", 0.85, "derived", note="no referral - applying directly through a portal")
    return Answer(None, 0.0, "profile", needs_review=True, note="referral details not in profile")


_THIS_EMPLOYER = {"us", "this company", "our company", "the company", "this organization", "this organisation",
                  "our organization", "our organisation", "this firm", "our firm", "this employer", "here"}


def _history_covers_career(p: Profile) -> bool:
    """Your work history accounts for all of your stated experience (so an employer missing from it is one you
    have not worked for - not merely one you did not list)."""
    from app.answers.knowledge import _merge_years, role_interval

    total = p.professional.total_experience_years
    roles = [w for w in p.professional.work_history if w.company]
    if total is None or not roles:
        return False
    intervals = [iv for w in roles if (iv := role_interval(w))]
    return len(intervals) == len(roles) and _merge_years(intervals) >= total - 0.5


def _is_a_name(question: str, phrase: str) -> bool:
    """The phrase is written as a proper name in the question ('Curai Health', not 'agile teams' / 'a startup')."""
    m = re.search(re.escape(phrase), question or "", re.I)
    words = m.group(0).split() if m else []
    return bool(words) and all(w[:1].isupper() or w.lower() in {"of", "and", "&", "the"} for w in words) \
        and words[0][:1].isupper() and not re.fullmatch(r"[A-Z]{2,3}", words[0])


def h_previous_employment(ctx, p, q):
    """Yes when the employer is in your work history. When it is not: No only if you have declared that history
    complete (professional.work_history_complete) - otherwise only you can confirm (never assumed)."""
    company = (ctx.job.company if ctx.job else "").lower().strip()
    q = re.sub(r"\s+or\s+(?:consulted|contracted|interned|freelanced|been\s+employed)(?=\s+(?:for|at|with|by)\b)", "", q)
    q = re.sub(r"\b(for|at|with|by)\s+(?:for|at|with|by)\b", r"\1", q)
    named = re.search(r"(?:work(?:ed|ing)?|employed|employee)\s+(for|at|with|by|of)\s+([a-z0-9&.\-/+# ]{2,40}?)(?:\s+(?:before|"
                      r"previously|earlier|in\s+the\s+past|in\s+any\s+capacity)|[?,]|$)", q)
    asked = named.group(2).strip() if named else ""

    def norm(x: str) -> str:
        return re.sub(r"\b(?:ltd|limited|pvt|private|inc|llp|technologies|solutions)\b\.?", "", x).strip(" .,")

    def same(a: str, b: str) -> bool:
        return bool(norm(a) and norm(b)) and (norm(a) in norm(b) or norm(b) in norm(a))

    this_employer = not asked or asked in _THIS_EMPLOYER or same(asked, company)
    target = company if this_employer else asked
    if re.search(r"applied|interview", q):
        return Answer(None, 0.0, "profile", needs_review=True, note="previous applications are not tracked in profile")
    history = [w.company.lower() for w in p.professional.work_history if w.company]
    if target and any(same(target, h) for h in history):
        return _yn(True, 0.92, note=f"{target} is in your work history")
    if not this_employer:
        if named.group(1) == "with" and _question_skills(asked):
            return None  # "Have you worked with Docker?" asks about a skill ("worked at GitLab" about an employer)
        if not _is_a_name(ctx.question, asked):  # "worked with agile teams / US clients": not an employer at all
            return Answer(None, 0.0, "profile", needs_review=True, yes_no=False,
                          note=f"'{asked}' is not in your work history - please confirm")
    if target and p.professional.work_history_complete is True:
        return _yn(False, 0.9, note=f"'{target}' is not in your work history, which you declared complete")
    hint = (" (it accounts for all your experience: set professional.work_history_complete to true to have this "
            "answered 'No' for you)") if p.professional.work_history_complete is None and _history_covers_career(p) else ""
    return Answer(None, 0.0, "profile", needs_review=True, yes_no=False,
                  note=f"'{target or 'this company'}' is not in your work history - please confirm{hint}")


def h_skill_yes_no(ctx, p, q):
    skills = _question_skills(q)
    if not skills or not (_wants_yes_no(ctx) or ctx.field_type in {"select", "radio"}):
        return None
    mine = {canonicalize(s) for s in p.skills.all}
    has = all(canonicalize(s) in mine for s in skills)
    # (a "No" is only stated for you when you declared the skills list complete; otherwise it is suggested and asked)
    return _yn(has, 0.88 if has or p.skills.complete is True else 0.7, note=f"skills: {', '.join(skills)}")


def h_skills_multi(ctx, p, q):
    """'Which of the following do you have hands-on experience with? (Select all that apply)': the listed options
    that name a skill in your profile. Nothing is ticked that your profile does not show, and 'None of the above'
    is never chosen for you."""
    opts = real_options(ctx.options)
    if ctx.field_type != "checkbox_group" or not opts:
        return None
    mine = {canonicalize(x) for x in p.skills.all}
    picks = [o for o in opts if (named := {canonicalize(x) for x in _question_skills(o)}) and named & mine]
    if not picks:
        return Answer(None, 0.0, "profile", needs_review=True,
                      note="none of the listed options is a skill in your profile - please choose")
    return Answer("; ".join(picks), 0.88, "profile", choices=picks, sources=["skills.primary", "skills.secondary"],
                  note=f"the options that name skills in your profile ({len(picks)} of {len(opts)})")


def h_skills_list(ctx, p, q):
    """A 'Skills' / 'Relevant skills React js, HTML 5...' box: the named skills you have, then your primary skills."""
    if real_options(ctx.options):
        return h_skills_multi(ctx, p, q)  # a bare "Skills" tick list (None for a single-choice dropdown)
    if re.search(r"\blearn|improv|lack|weak|missing|want\s+to|would\s+like|interested\s+in", q) or len(q.split()) > 14:
        return None  # not a request to list the skills you have
    primary = [x for x in p.skills.primary if x]
    if not primary:
        return _missing("skills.primary")
    by_canon = {canonicalize(x): x for x in p.skills.all}
    asked = [by_canon[c] for x in _question_skills(ctx.question) if (c := canonicalize(x)) in by_canon]
    skills = list(dict.fromkeys([*asked, *primary]))[:10]
    text, sources = ", ".join(skills), ["skills.primary", "skills.secondary"]
    if re.search(r"achievement|accomplishment|highlight", q):
        best = [h.strip().rstrip(".") + "." for h in p.professional.highlights if h.strip() and "YOUR_" not in h][:2]
        if not best:
            return None  # an achievement must come from you: left to the written-answer path
        text, sources = f"Key skills: {text}.\nAchievements: {' '.join(best)}", [*sources, "professional.highlights"]
    return Answer(text, 0.9, "profile", sources=sources,
                  note="skills from your profile" + (" (those the form names first)" if asked else ""))


def _preference(attr: str, what: str) -> Handler:
    def h(ctx: QuestionContext, p: Profile, q: str) -> Answer | None:
        v = getattr(p.work_preferences, attr)
        if v is None:
            return Answer(None, 0.0, "profile", needs_review=True, yes_no=None,
                          note=f"whether you are open to {what} is not in your profile (work_preferences.{attr})")
        if _wants_yes_no(ctx) or ctx.field_type in {"text", "select", "radio"}:
            return _yn(bool(v), 0.92, note=f"work_preferences.{attr}")
        return None
    h.__name__ = f"h_{attr}"
    return h


_LOCATION_OK = re.compile(r"(?:ok(?:ay)?|fine|comfortable|willing|open|happy|able|ready|agree\w*)\s+(?:with|to|for)\s+"
                          r"(?:\w+\s+){0,5}?(?:locations?|based)\b|^(?:job\s+|work\s+|office\s+|base\s+)?locations?\b", re.I)


def h_location_ok(ctx, p, q):
    """'Are you okay with the office location, Pune?' / 'Locations: Bangalore, Pune, Noida' (Yes / No): yes when one
    of the cities is where you live or prefer to work, else your willingness to relocate."""
    if not _wants_yes_no(ctx):
        return None
    cities = [c for c in parse_locations(q) if c not in {"remote", "anywhere"}]
    if not cities:
        return None
    wp = p.work_preferences
    if re.search(r"\boffice\b|on[\s-]?site|in[\s-]person", q) and not wp.open_to_onsite:
        return _yn(False, 0.88, note="you are not open to working from an office")
    pref = {c for x in wp.preferred_locations if (c := normalize_city(x))}
    current = normalize_city(p.personal.current_city)
    near = [c for c in cities if c in pref or c == current]
    if near:
        return _yn(True, 0.93, note=f"{near[0].title()} is where you live or one of your preferred locations")
    return _yn(wp.willing_to_relocate, 0.9, note="outside your preferred locations: your willingness to relocate")


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
    (r"\b(?:tele)?phone\b|\bmobile\b|contact\s+(?:number|no)|\bcell\b|whatsapp", h_phone, True),
    (r"linked\s*in", _personal("linkedin_url", "linkedin"), True),
    (r"git\s*hub", _personal("github_url", "github"), True),
    (r"portfolio|personal\s+(?:website|site|url)|^website|blog\b", h_portfolio, True),
    (r"postal|zip\s*code|\bzip\b|pin\s*code|pincode", _personal("postal_code", "postal"), True),
    # (never "Years of experience in state management?")
    (r"^(?!.*\b(?:experience|years?)\b)(?:.*\bstate\b(?!\s+(?:management|machines?))|.*province)", _personal("state", "state"), True),
    (r"^country$|country\s+of\s+residence|which\s+country|^country\s", _personal("country", "country"), True),
    (r"street|address\s+line|^address|residential\s+address", _personal("address", "address"), True),
    # ("current monthly salary" is not matched: in-hand pay is not your CTC / 12 - asked once, then remembered)
    (r"(?:current|present)\s+(?:(?:overall|total|gross|annual|yearly|fixed|base)\s+){0,2}(?:ctc|salary|compensation|package|remuneration)|\bcctc\b|current\s+fixed", h_salary("current"), False),
    (r"(?:annual|yearly|total)\s+(?:\w+\s+)?compensation\s+range|compensation\s+range|salary\s+range|"
     r"expected\s+(?:annual\s+|yearly\s+|base\s+|total\s+|overall\s+|gross\s+|fixed\s+)*(?:ctc|salary|compensation|package|pay|remuneration)|expected\s+annual|salary\s+expectation|"
     r"desired\s+(?:annual\s+|yearly\s+|base\s+|total\s+|overall\s+|gross\s+)*(?:salary|compensation|pay|ctc)|\bectc\b|(?:compensation|pay)\s+expectation|expected\s+fixed", h_salary("expected"), False),
    (r"salary\s+currency|currency", h_salary_currency, True),
    (r"notice\s+period|how\s+soon\s+can\s+you\s+join|joining\s+(?:time|date)|earliest\s+(?:start|joining|possible)|when\s+can\s+you\s+(?:start|join)|available\s+to\s+(?:start|join)|join\s+immediately|start\s+date|serving\s+notice|"
     r"^date\s+available|available\s+(?:from|date)\b|availability\s+date|date\s+of\s+(?:joining|availability)|"
     r"how\s+(?:soon|quickly|early)\s+(?:can|could|would)\s+you\s+(?:be\s+able\s+to\s+)?(?:start|join)|"
     r"when\s+(?:could|would)\s+you\s+(?:be\s+able\s+to\s+)?(?:start|join)|"
     r"how\s+many\s+(?:days|weeks|months)\s+(?:(?:can|will|would|do)\s+you|you\s+(?:can|will|would|need))\s+(?:\w+\s+){0,3}?(?:join|start)|"
     r"(?:days|time)\s+(?:required|needed)\s+to\s+join|join(?:ing)?\s+(?:in|within)\s+(?:how\s+many|\d+)\s+days|"
     r"lead\s+time\s+(?:to|for)\s+join|joining\s+lead\s+time|immediate\s+join(?:er|ers|ee|ing)?\b|how\s+(?:long|much\s+time)\s+(?:do|will|would)\s+you\s+"
     r"(?:need|take|require)\s+to\s+join", h_notice, False),
    (r"experience\s+level|seniority\s+level|career\s+level", h_experience_level, False),
    (r"^(?:total\s+|work\s+|overall\s+)?experience(?:\s*\((?:in\s+)?(?:years|yrs)\))?$|^experience\s+in\s+years$|whole\s+years", h_experience, True),
    (r"(?:minimum|at\s+least|min\.?)\s+(?:of\s+)?\d+(?:\.\d+)?\s*\+?\s*(?:years?|yrs?)\s+(?:of\s+)?(?:\w+\s+){0,3}?"
     r"(?:experience|exp)\b|\d+(?:\.\d+)?\s*\+?\s*(?:years?|yrs?)\s+(?:of\s+)?(?:\w+\s+){0,2}experience", h_experience, False),
    (r"only\s+\d+(?:\.\d+)?\s*\+?\s*(?:years?|yrs?)|\d+\s*\+\s*(?:years?|yrs?)\s+(?:only|apply)|less\s+experience\s+(?:don'?t|do\s+not)\s+apply",
     h_experience_threshold, False),
    (r"which\s+of\s+(?:the\s+following|these)\b.*(?:experience\s+(?:with|in|of)|worked\s+(?:with|on)|have\s+you\s+used|"
     r"do\s+you\s+(?:know|use)|familiar\s+with|proficient\s+(?:in|with)|skilled\s+(?:in|with))|"
     r"(?:technologies|tools|languages|frameworks|skills)\s+(?:do\s+you|have\s+you|you\s+have|are\s+you)\b", h_skills_multi, False),
    # a list of the company's openings ('Select position', 'Apply for', 'Job role'): only with options to choose from
    (r"^(?:select|choose|pick)\s+(?:the\s+|an?\s+|your\s+)?(?:job\s+)?(?:positions?|posts?|roles?|profiles?|openings?|vacanc(?:y|ies)|designations?)\b|"
     r"^(?:job\s+)?(?:positions?|posts?|roles?|openings?|vacanc(?:y|ies))(?:\s+(?:applied|applying)(?:\s+for)?)?$|^job\s+(?:roles?|profiles?)$|"
     r"^appl(?:y|ying|ied)(?:\s+for)?$|^(?:(?:job|position|post|role)\s+)?(?:you\s+are\s+)?applying\s+for$", h_position_choice, False),
    (r"^appl(?:y|ying|ied)\s+for$|^(?:(?:job|position|post|role)\s+)?(?:you\s+are\s+)?applying\s+for$", h_applied_title, True),
    (r"^subject(?:\s+line)?$|^subject\s+of\s+(?:the\s+|your\s+)?(?:application|message|e-?mail)$", h_subject, True),
    (r"^(?:(?:your|relevant|key|primary|technical|core|top|it|professional|main|other)\s+)*skills?(?:\s*set)?(?=$|[\s,(&/-])", h_skills_list, False),
    (r"^(?:select\s+)?(?:an?\s+)?(?:job\s+|role\s+|position\s+)?(?:category|department|domain|function(?:al\s+area)?|stream)$",
     h_role_category, True),
    (r"^(?:select\s+)?(?:an?\s+|your\s+)?(?:primary\s+|key\s+|main\s+)?(?:technology|technologies|tech\s+stack|skill\s?set|primary\s+skill)$",
     h_primary_technology, True),
    (r"^(?:your\s+)?(?:area|areas)\s+of\s+interests?$|^interests?$|^domain\s+of\s+interest$", h_area_of_interest, True),
    (r"how\s+many\s+(?:years|months)|years?\s+of\s+(?:\w+\s+){0,3}experience|(?:total|overall|relevant|work|professional)\s+(?:it\s+|industry\s+|work\s+|professional\s+)?experience|experience\s+in\s+(?:years|months)|\byoe\b|years\s+have\s+you", h_experience, False),
    (r"how\s+long\s+(?:have|did|do)\s+you\s+(?:been\s+)?(?:work(?:ed|ing)?|us(?:ed?|ing)|develop\w*|programm?\w*|cod(?:ed?|ing)|"
     r"buil(?:t|d\w*)|writ\w+)|since\s+when\s+have\s+you\s+(?:been\s+)?(?:work(?:ed|ing)|us(?:ed|ing))", h_how_long, False),
    (r"last\s+working\s+(?:day|date)|\blwd\b|relieving\s+date|date\s+of\s+(?:leaving|relieving)|last\s+day\s+(?:at|in|with)\s+(?:your\s+)?(?:current|present|last|previous)", h_last_working_day, False),
    (r"additional\s+months|months\s+of\s+experience\s+in\s+addition|\(additional\s+months\)", h_months_remainder, False),
    (r"\bfreshers?\b", h_fresher, False),
    (_CITY_CHOICE.pattern, h_location_choice, False),
    (r"interview\s+(?:availab|slot|schedul)|availab\w*\s+for\s+(?:a\s+|an\s+)?(?:virtual\s+|telephonic\s+|f2f\s+|"
     r"face[\s-]to[\s-]face\s+|in[\s-]person\s+)?(?:interview|discussion|call)", h_interview_availability, False),
    (r"relocat|(?:willing|ready|open|able|ok(?:ay)?|comfortable)\s+(?:to|with)\s+(?:shift(?:ing)?|mov(?:e|ing))\s+(?:to|base)\b|"
     r"\bshift(?:ing)?\s+(?:your\s+)?base\b", h_relocate, False),
    (_LOCATED_IN.pattern, h_located_in, False),
    (_LOCATION_OK.pattern, h_location_ok, False),
    (r"\b(?:night|rotational|rotating|evening|us|uk|est|pst|cst|emea|apac|24\s*[x*/]\s*7)\s+shifts?\b|\bshifts?\s+(?:timings?|work|hours|basis)|"
     r"work(?:ing)?\s+in\s+(?:\w+\s+)?shifts?\b|"
     r"(?:comfortable|willing|open|able|ok(?:ay)?|ready|flexible)\s+(?:\w+\s+){0,6}?shifts?\b(?!\s+(?:to|from|base|your|my)\b)|"
     r"us[\s-]based\s+hours|\b(?:us|uk|est|pst|cst|edt|pdt)\s+(?:business\s+|working\s+)?(?:hours|time\s*zones?|timings?)\b|night\s+hours",
     _preference("open_to_shifts", "shift work / other time zones"), False),
    (r"(?:willing|open|able|ready|comfortable)\s+(?:to\s+|with\s+)?travel|travel\s+(?:requirements?|required|up\s+to|\d+\s*%)|\d+\s*%\s+(?:of\s+)?travel",
     _preference("willing_to_travel", "travel"), False),
    # "This position is a contract role for 3 months, are you looking for a contract role?" (LinkedIn, run #48). Only
    # the question whether you take such roles - never the rate, duration or tax terms (W2 / C2C) of one.
    (r"(?:looking\s+for|open\s+(?:to|for)|ok(?:ay)?\s+(?:with|for)|fine\s+with|comfortable\s+with|interested\s+in|"
     r"willing\s+to\s+(?:work|take|accept|join)(?:\s+(?:on|in|as))?)\s+(?:\w+\s+){0,4}?"
     r"(?:contract(?:ual)?|contract[\s-]to[\s-]hire|c2h|temporary|fixed[\s-]term)\b(?!\s+(?:rate|duration|terms))",
     _preference("open_to_contract", "contract / temporary roles"), False),
    (r"preferred\s+(?:work\s+|job\s+)?(?:locations?|cit(?:y|ies))|looking\s+for\s+jobs?\s+in\b|locations?\s+preferences?|"
     r"cit(?:y|ies)\s+you\s+(?:are\s+)?(?:open\s+to|prefer)", h_preferred_location, False),
    (r"^(?:current\s+)?city\b|current\s+(?:city|location)|^location$|^location\s|where\s+are\s+you\s+(?:currently\s+)?(?:based|located|living)|where\s+will\s+you\s+be\s+(?:physically\s+)?(?:based|located|working\s+from)|place\s+of\s+residence|city\s+of\s+residence", h_city, False),
    (r"(?:work|working)\s+from\s+(?:the\s+|our\s+)?office|on[\s-]?site|in[\s-]office|in[\s-]person|come\s+to\s+(?:the\s+|our\s+)?office|\bwfo\b|hybrid|commut|work\s+remotely|remote\s+work", h_onsite, False),
    (r"sponsor", h_sponsorship, False),
    (r"\bvisa\b|work\s+permit|green\s*card|\bh-?1b\b", h_visa, False),
    (r"authori[sz]ed\s+to\s+work|legally\s+(?:authori[sz]ed|eligible|permitted)|right\s+to\s+work|work\s+permit|eligible\s+to\s+work|work\s+authori[sz]ation", h_authorization, False),
    (r"^(?:citizenship|nationality)(?:\s+status)?$|country\s+of\s+(?:citizenship|nationality)|(?:your|what\s+is\s+your)\s+(?:citizenship|nationality)|are\s+you\s+an?\s+(?:\w+\s+)?citizen|citizen\s+of\b", h_citizenship, False),
    (r"(?:at\s+least|over|above|older\s+than)\s+18|18\s+years\s+(?:of\s+age|or\s+older|old)|legal\s+(?:working\s+)?age", h_adult, False),
    (r"graduat\w*\s+year|year\s+of\s+(?:graduation|passing|completion)|passing\s+year|passed\s+out", h_grad_year, False),
    (r"\bgpa\b|\bcgpa\b|percentage|\bgrade\b|\bmarks\b", h_grade, True),
    (r"field\s+of\s+study|\bmajor\b|speciali[sz]ation|discipline|\bstream\b", h_field_of_study, True),
    (r"(?:university|college|school|institut\w*)(?:\s+name)?$|name\s+of\s+(?:the\s+)?(?:university|college|institut)|^school|^university|^college", h_institution, True),
    (r"highest\s+(?:level\s+of\s+)?(?:education|qualification|degree)|\bdegree\b|\bqualification\b|level\s+of\s+education|bachelor'?s|master'?s", h_degree, False),
    (r"current\s+(?:company|employer|organi[sz]ation)|present\s+(?:company|employer|organi[sz]ation)|(?:company|employer)\s+name|most\s+recent\s+(?:company|employer)|current\s+or\s+(?:most\s+)?recent\s+(?:company|employer)", h_current_company, False),
    (r"(?:job\s+title|position|role|job)\s+(?:you\s+are\s+|you're\s+)?(?:applying|applied)\s+(?:for|to)|(?:position|role|job)\s+applied\s+for|applying\s+for\s+(?:the\s+)?(?:position|role)\s+of", h_applied_title, False),
    (r"current\s+(?:job\s+)?(?:title|designation|role|position)|^(?:job\s+)?title$|designation|most\s+recent\s+(?:job\s+)?title|current\s+or\s+(?:most\s+)?recent\s+(?:job\s+)?title", h_current_title, False),
    (r"\bjob\s+title\b|^title\b|^your\s+title$", h_current_title, True),
    (r"currently\s+(?:employed|working)", h_employed, False),
    (r"\benglish\b", h_english, False),
    (r"\bgender\b|\bsex\b", _diversity("gender"), False),
    (r"pronoun", _diversity("pronouns"), False),
    (r"veteran|military|armed\s+forces", _diversity("veteran_status"), False),
    (r"disabilit|handicap|differently[\s-]abled", _diversity("disability_status"), False),
    (r"ethnic|\brace\b|racial|hispanic|latino", _diversity("ethnicity"), False),
    (r"how\s+did\s+you\s+(?:hear|find|learn|come\s+across)|where\s+did\s+you\s+(?:hear|find|see)|source\s+of\s+(?:application|referral)|referral\s+source|^source$", h_source, False),
    (r"referr(?:ed|al)\s+by|employee\s+referral|referral\s+(?:name|code)|who\s+referred|were\s+you\s+referred|\brefer(?:red)?\s+you\b|"
     r"referred\s+(?:to|for)\s+this", h_referral, False),
    (r"(?:previously|formerly|ever|before)(?:,?\s+or\s+currently)?,?\s+(?:work(?:ed)?|been\s+employed|employed|applied|interviewed)\b|"
     r"worked\s+(?:for|at|with)\s+(?:us|this\s+company)\s+before|worked\s+here\s+before|"
     r"(?:current\s+or\s+)?(?:former|past|previous|ex)[\s-]employee|have\s+you\s+(?:ever\s+)?worked\s+(?:for|at|with)\b|"
     r"(?:been\s+)?employed\s+(?:by|at|with)\s+(?:us|this\s+company)", h_previous_employment, False),
    (r"privacy|terms\s+(?:and|&)\s+conditions|\bconsent\b|i\s+agree|acknowledge|i\s+certify|information\s+(?:provided\s+)?is\s+(?:true|accurate)|"
     r"(?:you|i)\s+agree\s+(?:with|to)\s+(?:the\s+)?(?:storage|processing|handling|collection|use)\b", h_consent, False),
    (r"^(?:role\s+|job\s+|position\s+|work\s+)?description$|^(?:key\s+|main\s+)?responsibilities$|^(?:role\s+)?summary$|^what\s+did\s+you\s+do", h_role_description, True),
    (r"(?:links?|urls?)\s+(?:to|of|for)\s+(?:your\s+|some\s+of\s+your\s+)?(?:\w+\s+){0,3}(?:work|projects?|portfolio)|"
     r"work\s+samples?|samples?\s+of\s+your\s+work|portfolio\s+(?:links?|url)|share\s+(?:your\s+)?(?:portfolio|github|work)\b",
     h_work_links, False),
    (r"tell\s+(?:us|me)\s+about\s+yourself|about\s+yourself|describe\s+yourself|introduce\s+yourself|professional\s+summary|^summary$|"
     r"^(?:(?:short|brief|professional|your)\s+)*bio(?:graphy)?$|^about\s+(?:you|me)$|^profile\s+summary$", h_about, False),
    (r"why\s+(?:are\s+you|do\s+you|would\s+you|should\s+we|this|our|join|us)|interest(?:ed)?\s+in|motivat|cover\s+letter|what\s+makes\s+you|additional\s+information|anything\s+else|message\s+to|describe\s+(?:a|your|an)|tell\s+us\s+(?:about|why)|explain|elaborate", h_open, False),
    (r"(?:do\s+you\s+have|have\s+you|are\s+you)\s+(?:any\s+)?(?:[\w.+#/-]+\s+){0,4}?(?:experience|exposure|worked|proficien|familiar|comfortable|skilled|knowledge|expert)|\bproficien|knowledge\s+of|familiar\s+with|hands[\s-]on", h_skill_yes_no, False),
]

_COMPILED = [(re.compile(rx, re.I), h, label_only) for rx, h, label_only in RULES]
_LABEL_PREFIX = re.compile(r"^(?:(?:what\s+is|what's|please\s+(?:enter|provide|share|mention)|enter|provide|share|mention)\s+(?:your\s+)?|your\s+)")
_QUESTION_START = re.compile(r"^(?:do|does|are|is|have|has|would|will|can|could|how|what|why|which|did|were|please|describe|tell)\b")


def resolve(ctx: QuestionContext, profile: Profile) -> Answer | None:
    q = repair_label(ctx.question)
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
                    ans.rule = handler.__name__
                return ans
    # Fallback: a skill named in a field that wants a number - "React (years)" number box, a "React experience" /
    # "Total React experience" dropdown of years or ranges (never a free-text box: that is a written answer)
    if _question_skills(q) and ctx.field_type not in {"textarea", "contenteditable"}:
        from app.answers.options import numeric_options

        wants_number = ctx.field_type == "number" or numeric_options(ctx.options)
        if wants_number or (re.search(r"\bexperience\b|\byears?\b|\byrs?\b|\byoe\b", q) and not ctx.options
                            and ctx.field_type in {"text", "tel"}):
            return h_experience(ctx, profile, q)
    # The options say what is asked when the label does not (an unlabeled dropdown of 'Fresher / 1+ Experience /
    # 5+ Experience'): overall experience, matched by value
    opts = real_options(ctx.options)
    if opts and not _question_skills(q) and sum(bool(_EXPERIENCE_OPTION.search(o)) for o in opts) >= max(2, len(opts) * 0.6):
        from app.answers.options import numeric_options

        if numeric_options(opts):
            ans = h_experience(ctx, profile, "total experience")
            if ans is not None:
                ans.note = (ans.note + "; " if ans.note else "") + "options are experience levels"
            return ans
    return None


_EXPERIENCE_OPTION = re.compile(r"\bfresher\b|\bexperience[d]?\b|\bexp\b|\byrs?\s+exp", re.I)
