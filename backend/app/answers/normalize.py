"""Fit a numeric answer to the control that will receive it - decided from the DOM, never by the LLM.

The profile keeps its canonical value (e.g. 6.5 years). A form field may want another representation of that same
value: ``<input type=number>`` without ``step`` (HTML default 1) or ``inputmode=numeric`` or ``pattern=[0-9]+``
or a "whole number" validation message accepts integers only -> 6 (floored: experience is never overstated);
``step=0.5``/``any``/``inputmode=decimal`` -> 6.5; a value outside ``min``/``max`` cannot be entered truthfully ->
the field is left for you.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

NUMBER = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*$")
INTEGER_HINT = re.compile(
    r"whole\s+number|\binteger\b|without\s+(?:a\s+)?decimals?|no\s+decimals?|decimals?\s+(?:are\s+|is\s+)?not\s+allowed|"
    r"digits?\s+only|only\s+(?:whole\s+)?(?:numbers|digits)|round\s+number|in\s+whole\s+years|full\s+years", re.I)
DECIMAL_HINT = re.compile(r"decimal\s+number|decimals?\s+(?:are\s+)?allowed|e\.?\s?g\.?\s*:?\s*\d+\.\d|\b\d+\.\d+\s*years", re.I)


@dataclass
class Fitted:
    value: str | None  # what to type; None: the value cannot be represented truthfully in this control
    note: str = ""
    changed: bool = False


def _fmt(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:.2f}".rstrip("0").rstrip(".")


def _num(text: str) -> float | None:
    try:
        return float(text) if str(text).strip() not in {"", "any"} else None
    except ValueError:
        return None


def _pattern_integer_only(pattern: str) -> bool | None:
    """True / False when the HTML pattern decides it, None when it does not (or cannot be read)."""
    if not pattern:
        return None
    try:
        rx = re.compile(pattern)
    except re.error:
        return None
    if rx.fullmatch("6.5"):
        return False
    if rx.fullmatch("6"):
        return True
    return None


def fit_number(raw: str | None, *, field_type: str = "text", step: str = "", min_: str = "", max_: str = "",
               pattern: str = "", inputmode: str = "", hint: str = "") -> Fitted:
    """Representation of the numeric answer ``raw`` for this control. Non-numeric answers pass through."""
    if raw is None:
        return Fitted(None)
    m = NUMBER.match(str(raw))
    if not m:
        return Fitted(raw)
    v = float(m.group(1))
    integer_only: bool | None = None
    step_v = None
    if field_type == "number":
        if step.strip().lower() == "any":
            integer_only = False
        else:
            step_v = _num(step) or 1.0  # HTML default step is 1: 6.5 is invalid there
            integer_only = float(step_v).is_integer()
    if integer_only is None:
        integer_only = _pattern_integer_only(pattern)
    if integer_only is None and inputmode:
        integer_only = {"numeric": True, "decimal": False}.get(inputmode.strip().lower())
    if integer_only is None and hint:
        if INTEGER_HINT.search(hint):
            integer_only = True
        elif DECIMAL_HINT.search(hint):
            integer_only = False
    lo, hi = _num(min_), _num(max_)
    out = v
    if step_v and not float(step_v).is_integer():  # e.g. step 0.5 -> multiples of 0.5, rounded down
        out = math.floor(v / step_v + 1e-9) * step_v
    elif integer_only:
        out = math.floor(v + 1e-9)
    if hi is not None and out > hi:
        return Fitted(None, f"{_fmt(v)} is above the field's maximum {_fmt(hi)}")
    if lo is not None and out < lo:
        return Fitted(None, f"{_fmt(v)} is below the field's minimum {_fmt(lo)}")
    if out != v:
        return Fitted(_fmt(out), f"the field takes whole numbers: {_fmt(v)} entered as {_fmt(out)} (not rounded up)",
                      changed=True)
    return Fitted(_fmt(v))


def refit_after_rejection(raw: str, message: str, **dom) -> Fitted:
    """The site rejected ``raw``: read its message once and return another representation of the same value,
    or None when the message does not say what it wants (then you are asked - no repeated guessing)."""
    m = NUMBER.match(str(raw or ""))
    if not m:
        return Fitted(None)
    hint = f"{dom.pop('hint', '')} {message}"
    fitted = fit_number(raw, hint=hint, **dom)
    if fitted.value is not None and fitted.value != _fmt(float(m.group(1))):
        return fitted
    if INTEGER_HINT.search(message or ""):
        return fit_number(raw, field_type="text", hint="whole number")
    # "Invalid input" says nothing about the format. For a decimal the only other representation of the same value
    # is the whole number (6.5 -> 6, never rounded up) - unless the page says decimals are fine. Tried once.
    if ("." in m.group(1) and GENERIC_INVALID.search(message or "") and str(dom.get("step") or "") != "any"
            and not DECIMAL_HINT.search(hint)):
        whole = fit_number(raw, field_type="text", hint="whole number")
        if whole.value is not None:
            whole.note = f"the site rejected {raw} ('{message.strip().splitlines()[0][:60]}'): entered the whole number {whole.value}"
            return whole
    return Fitted(None, f"the site rejected {raw}: {message[:120]}")


GENERIC_INVALID = re.compile(r"^\s*(?:invalid(?:\s+(?:input|value|entry|answer|format|number))?|(?:please\s+)?enter\s+a\s+"
                             r"valid\s+(?:answer|value|number|input|response)|not\s+a\s+valid\s+(?:value|answer|number|input))"
                             r"\b[.!]?", re.I)
