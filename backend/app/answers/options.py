"""Mapping an answer onto the options of a select / radio question."""

from __future__ import annotations

import math
import re

from rapidfuzz import fuzz, process

PLACEHOLDER_OPTION = re.compile(r"^\s*(?:--+|select(?:\s+an?)?(?:\s+option)?\.*|please\s+select.*|choose.*|none\s+selected|)\s*$", re.I)
DECLINE_OPTION = re.compile(r"decline|prefer\s+not|don'?t\s+wish|do\s+not\s+wish|not\s+to\s+(?:say|disclose|answer|self)|rather\s+not|choose\s+not|wish\s+not", re.I)
YES_WORDS = {"yes", "y", "true", "i do", "i am", "i agree", "agree", "i have", "yes i am", "yes, i am", "yes i do",
             "i accept", "accept", "i confirm", "confirm", "willing", "available"}
NO_WORDS = {"no", "n", "false", "i do not", "i don't", "i am not", "i'm not", "no i am not", "no, i am not",
            "not willing", "i have not", "i haven't"}


def real_options(options: list[str]) -> list[str]:
    return [o for o in options if o and not PLACEHOLDER_OPTION.match(o)]


_NOT_A_LABEL = re.compile(r"^(?:on|[\W_]+)$")  # the DOM default value of an unlabeled radio / checkbox; separators


def choice_labels(options: list[str] | None) -> list[str]:
    """The choices of a question as shown to you: trimmed, without placeholders ('Select...'), without labels that
    carry no meaning ('on', '──────'), each one once, in the form's order. Two options with the same text cannot be
    told apart (an answer is matched by its text), so they are one choice."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in options or []:
        label = re.sub(r"\s+", " ", str(raw or "")).strip()
        if not label or label in seen or PLACEHOLDER_OPTION.match(label) or _NOT_A_LABEL.match(label):
            continue
        seen.add(label)
        out.append(label)
    return out


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w+#.' ]", " ", s.lower())).strip()


def parse_option_range(option: str, unit_scale: float = 1.0) -> tuple[float, float] | None:
    """'3-5 years' -> (3,5); '5+ years' -> (5, inf); 'Less than 1 year' -> (0, 1)."""
    o = option.lower().replace("–", "-").replace("—", "-").replace("\u00a0", " ")
    if re.search(r"\b(?:fresher|no experience|none)\b", o):
        return 0.0, 0.0
    # "6 Years 6 Months (6.6)", "7 Years (7.0)", "0 Year 2 Months": an exact length, months counted as twelfths
    m = re.match(r"^\s*(\d+)\s*(?:years?|yrs?)\b(?:\s*(?:and\s*)?(\d+)\s*(?:months?|mos?)\b)?(?:\s*\([\d.]+\))?\s*$", o)
    if m and unit_scale == 1.0:
        v = int(m.group(1)) + int(m.group(2) or 0) / 12
        return v, v
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:-|to)\s*(\d+(?:\.\d+)?)", o)
    if m:
        return float(m.group(1)) * unit_scale, float(m.group(2)) * unit_scale
    m = re.search(r"(?:more\s+than|over|above|greater\s+than|at\s+least|minimum|>=?|≥)\s*(\d+(?:\.\d+)?)", o)
    if m:
        return float(m.group(1)) * unit_scale, math.inf
    m = re.search(r"(\d+(?:\.\d+)?)\s*\+", o)
    if m:
        return float(m.group(1)) * unit_scale, math.inf
    m = re.search(r"(?:less\s+than|under|below|up\s*to|upto|within|<)\s*(\d+(?:\.\d+)?)", o)
    if m:
        return 0.0, float(m.group(1)) * unit_scale
    m = re.search(r"(\d+(?:\.\d+)?)", o)
    if m:
        v = float(m.group(1)) * unit_scale
        return v, v
    return None


def choose_numeric(value: float, options: list[str], unit_scale: float = 1.0) -> str | None:
    """The option that contains ``value`` (narrowest range first; '5+' over '1+'). When none does, the highest option
    that does not exceed it ('6.5' -> '6', '6 years'; '1-3 / 4-6 / 7-9' -> '4-6'): an option never overstates the value."""
    containing: tuple[float, float, str] | None = None
    below: tuple[float, float, str] | None = None
    for opt in real_options(options):
        rng = parse_option_range(opt, unit_scale)
        if rng is None:
            continue
        lo, hi = rng
        if lo <= value <= hi:
            width = hi - lo  # narrowest first; of open ranges ('1+', '5+') the one with the highest floor
            if containing is None or (width, -lo) < (containing[0], -containing[1]):
                containing = (width, lo, opt)
        elif hi < value and not (hi == 0 and value >= 1) and (below is None or hi > below[0]
                                                              or (hi == below[0] and hi - lo < below[1])):
            below = (hi, hi - lo, opt)  # ('Fresher' is no understatement of 7 years: it is untrue)
    if containing:
        return containing[2]
    if below:
        return below[2]
    if value >= 1:  # 'Experienced' / 'Fresher'
        return next((o for o in real_options(options) if EXPERIENCED_OPTION.search(o) and parse_option_range(o) is None), None)
    return None


EXPERIENCED_OPTION = re.compile(r"^\s*(?:i\s+am\s+)?(?:an?\s+)?(?:experienced|working\s+professional|professional)\b", re.I)


def numeric_options(options: list[str]) -> bool:
    """Options that are numbers / ranges ('0'...'10', '3-5 years', '5+'): matched by value, never by spelling."""
    opts = real_options(options)
    return bool(opts) and sum(parse_option_range(o) is not None for o in opts) >= max(2, len(opts) * 0.6)


def choose_yes_no(yes: bool, options: list[str]) -> str | None:
    words = YES_WORDS if yes else NO_WORDS
    opts = real_options(options)
    for opt in opts:
        if _norm(opt) in words:
            return opt
    prefix = "yes" if yes else "no"
    for opt in opts:
        if _norm(opt).startswith(prefix + " ") or _norm(opt).startswith(prefix + ","):
            return opt
    return None


def choose_decline(options: list[str]) -> str | None:
    return next((o for o in real_options(options) if DECLINE_OPTION.search(o)), None)


def split_choices(value: str, options: list[str] | None = None) -> list[str]:
    """The options a multi-choice answer names. An option may itself contain a comma ('Gurugram, Haryana'): the
    form's own option texts are recognised first, then 'A; B', then 'A, B'."""
    text = (value or "").strip()
    known = {o.strip().lower() for o in options or [] if o and o.strip()}
    if not text:
        return []
    if text.lower() in known:
        return [text]
    if ";" in text:
        return [v.strip() for v in text.split(";") if v.strip()]
    parts = [p.strip() for p in text.split(",")]
    out: list[str] = []
    i = 0
    while i < len(parts):
        for j in range(len(parts), i, -1):  # the longest run of parts that is one of the options
            joined = ", ".join(parts[i:j])
            if j - i > 1 and joined.lower() in known:
                out.append(joined)
                i = j
                break
        else:
            if parts[i]:
                out.append(parts[i])
            i += 1
    return out


def choose_option(value: str, options: list[str], *, numeric: float | None = None, yes_no: bool | None = None,
                  unit_scale: float = 1.0, min_score: int = 85) -> str | None:
    opts = real_options(options)
    if not opts:
        return None
    if yes_no is not None:
        picked = choose_yes_no(yes_no, opts)
        if picked:
            return picked
    if numeric is not None:
        picked = choose_numeric(numeric, opts, unit_scale)
        if picked:
            return picked
        if numeric_options(opts):
            return None  # '6.5' must never fuzzy-match '5': no option fits this value
    if value is None:
        return None
    v = _norm(str(value))
    if not v:
        return None
    for opt in opts:
        if _norm(opt) == v:
            return opt
    if v in YES_WORDS or v in NO_WORDS:
        picked = choose_yes_no(v in YES_WORDS, opts)
        if picked:
            return picked
    contained = [o for o in opts if v in _norm(o) or (_norm(o) and _norm(o) in v and len(_norm(o)) > 2)]
    if len(contained) == 1:
        return contained[0]
    match = process.extractOne(v, {o: _norm(o) for o in opts}, scorer=fuzz.WRatio)
    if match and match[1] >= min_score:
        return match[2]
    return None
