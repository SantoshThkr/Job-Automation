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


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w+#.' ]", " ", s.lower())).strip()


def parse_option_range(option: str, unit_scale: float = 1.0) -> tuple[float, float] | None:
    """'3-5 years' -> (3,5); '5+ years' -> (5, inf); 'Less than 1 year' -> (0, 1)."""
    o = option.lower().replace("–", "-").replace("—", "-")
    if re.search(r"\b(?:fresher|no experience|none)\b", o):
        return 0.0, 0.0
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:-|to)\s*(\d+(?:\.\d+)?)", o)
    if m:
        return float(m.group(1)) * unit_scale, float(m.group(2)) * unit_scale
    m = re.search(r"(?:more\s+than|over|above|greater\s+than|at\s+least|minimum)\s*(\d+(?:\.\d+)?)", o)
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
    best: tuple[float, str] | None = None
    for opt in real_options(options):
        rng = parse_option_range(opt, unit_scale)
        if rng is None:
            continue
        lo, hi = rng
        if lo <= value <= hi:
            width = hi - lo
            if best is None or width < best[0]:
                best = (width, opt)
    return best[1] if best else None


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
