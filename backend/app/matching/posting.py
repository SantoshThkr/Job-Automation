"""When was a job posted, and is it recent enough to apply to?

Background (2026-10-14): Naukri ignored the "posted within 2 days" filter of its search, and nothing checked the
age of a job afterwards. 673 of 1 314 Naukri listings had been labelled "3+ weeks ago" when they were found, 27 of
58 Naukri applications went to such jobs, and one approved yesterday had been posted on 2026-06-25 (its own page
said so: ``datePosted``) - 111 days before the application, three weeks after its listing had expired.

Two kinds of evidence exist:

* **the portal's own date on the job page** (JSON-LD ``datePosted``, sometimes ``validThrough``) - reliable;
* **a relative label in a listing** ("2 hours ago", "1 day ago", "3+ weeks ago") - reliable only together with the
  moment it was read, and only to its unit: portals round down, so "1 week ago" means 7 to 13 days and "2 days ago"
  48 to 71 hours (``spread_s``). A label with a "+" is a lower bound: "3+ weeks ago" means at least 21 days, possibly
  months. A posting whose possible dates straddle the window's edge is 'unknown' - never applied to automatically.

Never used as a posting date: when the assistant found the job, imported it, or last updated its record.

Dates are compared in this machine's time zone (a date without a time is a calendar day here); stored as naive UTC.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

_SECONDS = {"minute": 60, "hour": 3600, "day": 86400, "week": 7 * 86400, "month": 30 * 86400, "year": 365 * 86400}
_UNIT = {"min": "minute", "mins": "minute", "minute": "minute", "minutes": "minute", "hr": "hour", "hrs": "hour",
         "hour": "hour", "hours": "hour", "day": "day", "days": "day", "wk": "week", "wks": "week", "week": "week",
         "weeks": "week", "mo": "month", "mos": "month", "month": "month", "months": "month", "yr": "year",
         "yrs": "year", "year": "year", "years": "year"}
_WORD_NUMBER = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "few": 3,
                "several": 3}
_RELATIVE = re.compile(r"\b(\d{1,3}|an?|one|two|three|four|five|six|few|several)\s*(\+)?\s*(min(?:ute)?s?|hrs?|hours?|"
                       r"days?|wks?|weeks?|mos?|months?|yrs?|years?)\s*(\+)?\s*ago\b", re.I)
_JUST_NOW = re.compile(r"\b(?:just\s+now|moments?\s+ago|today|this\s+(?:morning|afternoon|evening)|"
                       r"within\s+the\s+last\s+24\s+hours|new\s+today)\b", re.I)
_YESTERDAY = re.compile(r"\byesterday\b", re.I)
_MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                            "dec"])}
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?\s*(Z|[+-]\d{2}:?\d{2})?)?\b")
_DAY_MONTH_YEAR = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?[\s\-/.]+([A-Za-z]{3,9})[\s\-/.,]+(\d{4})\b")
_MONTH_DAY_YEAR = re.compile(r"\b([A-Za-z]{3,9})[\s\-/.]+(\d{1,2})(?:st|nd|rd|th)?,?[\s\-/.]+(\d{4})\b")
_NUMERIC = re.compile(r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})\b")


def local_tz():
    """This machine's time zone (where "today" and calendar dates are counted)."""
    return datetime.now().astimezone().tzinfo


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass(frozen=True)
class Posted:
    at: datetime  # naive UTC; for an open-ended label the latest moment the job can have been posted
    open_ended: bool = False  # "3+ weeks ago": posted at ``at`` or (perhaps much) earlier
    date_only: bool = False  # a calendar date without a time of day
    source: str = ""  # "page" (the portal's own date) | "label" (a relative label, read at a known moment)
    spread_s: int = 0  # a label is precise to its unit: posted between ``at - spread_s`` and ``at``

    @property
    def earliest(self) -> datetime:
        return self.at - timedelta(seconds=self.spread_s)

    def local_date(self) -> date:
        return self.at.replace(tzinfo=UTC).astimezone(local_tz()).date()

    def earliest_date(self) -> date:
        return self.earliest.replace(tzinfo=UTC).astimezone(local_tz()).date()


def _local_noon_utc(d: date) -> datetime:
    """A calendar day as a moment: noon there (a day boundary in another zone never moves it to another date)."""
    return datetime.combine(d, time(12, 0), tzinfo=local_tz()).astimezone(UTC).replace(tzinfo=None)


def parse_date(text: str, *, now: datetime | None = None) -> Posted | None:
    """An absolute date: ISO 8601 ("2026-06-25", "2026-06-25T08:30:00+05:30"), "25 Jun 2026", "Jun 25, 2026", or a
    numeric date whose day/month order is unambiguous ("25/06/2026"; "05/06/2026" is not - None). A date in the
    future, or before 2000, is not believed."""
    if not text:
        return None
    now = now or utcnow()
    found: Posted | None = None
    if m := _ISO.search(text):
        y, mo, d, hh, mm, ss, zone = m.groups()
        try:
            if hh is None:
                found = Posted(_local_noon_utc(date(int(y), int(mo), int(d))), date_only=True, source="page")
            else:
                moment = datetime(int(y), int(mo), int(d), int(hh), int(mm), int(ss or 0))
                if zone:
                    sign = 1 if zone[0] == "+" else -1
                    digits = zone.replace(":", "")
                    offset = timedelta(hours=int(digits[1:3]), minutes=int(digits[3:5])) if zone != "Z" else timedelta(0)
                    moment = moment - sign * offset if zone != "Z" else moment
                else:  # a time without a zone: this machine's time
                    moment = moment.replace(tzinfo=local_tz()).astimezone(UTC).replace(tzinfo=None)
                found = Posted(moment, source="page")
        except ValueError:
            return None
    else:
        day_month = None
        if m := _DAY_MONTH_YEAR.search(text):
            day_month = (int(m.group(1)), _MONTHS.get(m.group(2)[:3].lower()), int(m.group(3)))
        elif m := _MONTH_DAY_YEAR.search(text):
            day_month = (int(m.group(2)), _MONTHS.get(m.group(1)[:3].lower()), int(m.group(3)))
        elif m := _NUMERIC.search(text):
            a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if a > 12 >= b:
                day_month = (a, b, y)
            elif b > 12 >= a:
                day_month = (b, a, y)
            else:
                return None  # 05/06/2026: May or June - not reliable
        if not day_month or not day_month[1]:
            return None
        try:
            found = Posted(_local_noon_utc(date(day_month[2], day_month[1], day_month[0])), date_only=True, source="page")
        except ValueError:
            return None
    if found is None or found.at.year < 2000 or found.at > now + timedelta(days=1):
        return None
    return found


def parse_label(text: str, *, seen: datetime) -> Posted | None:
    """A listing's relative label, read at ``seen`` (naive UTC): "Posted: 3 days ago", "1 day ago", "3+ weeks ago",
    "30+ days ago", "Reposted 2 weeks ago", "Just now", "Few hours ago", "Today", "Yesterday". An absolute date in
    the text is read as one. Anything else - None (unknown, never guessed)."""
    if not text or not text.strip():
        return None
    if absolute := parse_date(text, now=seen):
        return absolute
    if m := _RELATIVE.search(text):
        amount, plus_before, unit, plus_after = m.groups()
        n = int(amount) if amount.isdigit() else _WORD_NUMBER.get(amount.lower(), 1)
        seconds = n * _SECONDS[_UNIT[unit.lower()]]
        step = _SECONDS[_UNIT[unit.lower()]]  # rounded down to whole units: up to one more unit earlier
        if amount.lower() == "few" and _UNIT[unit.lower()] == "hour":
            return Posted(seen, source="label", spread_s=_SECONDS["day"])  # "Few hours ago": within the last day
        return Posted(seen - timedelta(seconds=seconds), open_ended=bool(plus_before or plus_after), source="label",
                      spread_s=step)
    if re.search(r"\b(?:just\s+now|moments?\s+ago)\b", text, re.I):
        return Posted(seen, source="label", spread_s=_SECONDS["hour"])
    if _JUST_NOW.search(text) or re.search(r"\bfew\s+(?:minutes|hours)\s+ago\b", text, re.I):
        return Posted(seen, source="label", spread_s=_SECONDS["day"])  # "Today", "Few hours ago"
    if _YESTERDAY.search(text):
        return Posted(seen - timedelta(days=1), source="label", spread_s=_SECONDS["day"])
    return None


def label_spread(text: str) -> int:
    """How precise a relative label is, in seconds (0 for an absolute date or no label)."""
    p = parse_label(text or "", seen=datetime(2026, 1, 1))
    return p.spread_s if p is not None else 0


def _precision(spread_s: int) -> str:
    for name, secs in (("year", 365 * 86400), ("month", 30 * 86400), ("week", 7 * 86400), ("day", 86400)):
        if spread_s >= secs:
            return f"a {name}"
    return "an hour"


@dataclass(frozen=True)
class Window:
    """Which jobs are recent enough: the last ``days`` days, a custom range of calendar days (inclusive), or every
    date (``days`` None and no range - only when you chose it)."""
    days: int | None = 7
    start: date | None = None
    end: date | None = None

    @property
    def all_dates(self) -> bool:
        return self.days is None and self.start is None and self.end is None

    def label(self) -> str:
        if self.start or self.end:
            return f"posted {self.start.isoformat() if self.start else 'any day'} to {self.end.isoformat() if self.end else 'today'}"
        if self.days is None:
            return "all dates"
        return "posted in the last 24 hours" if self.days == 1 else f"posted in the last {self.days} days"

    def search_days(self) -> int | None:
        """What a portal's own "posted within" search filter should ask for (None: no filter)."""
        if self.start:
            return max(1, (datetime.now(local_tz()).date() - self.start).days + 1)
        return self.days


DEFAULT_DAYS = 7
CHOICES = (1, 3, 7, 14, 30, 60, 90)  # the dashboard's "last N days" options


def window_from(stored: dict | None) -> Window:
    """The window saved in the agent state ({} or nothing saved: the last 7 days)."""
    stored = stored or {}
    if stored.get("all"):
        return Window(days=None)
    if stored.get("start") or stored.get("end"):
        try:
            start = date.fromisoformat(stored["start"]) if stored.get("start") else None
            end = date.fromisoformat(stored["end"]) if stored.get("end") else None
        except (TypeError, ValueError):
            return Window(days=DEFAULT_DAYS)
        return Window(days=None, start=start, end=end)
    days = stored.get("days")
    return Window(days=int(days)) if isinstance(days, int) and days > 0 else Window(days=DEFAULT_DAYS)


def window_to_dict(window: Window) -> dict:
    if window.start or window.end:
        return {"start": window.start.isoformat() if window.start else None,
                "end": window.end.isoformat() if window.end else None}
    if window.days is None:
        return {"all": True}
    return {"days": window.days}


def verdict(posted: Posted | None, window: Window, *, now: datetime | None = None) -> tuple[str, str]:
    """('in' | 'out' | 'unknown', why). 'unknown' is never applied to automatically."""
    now = now or utcnow()
    if window.all_dates:
        return "in", "all posting dates are allowed (you chose 'All dates')"
    if posted is None:
        return "unknown", f"posting date unknown - not applied automatically while the job-recency window is {window.label()}"
    shown = describe(posted, now=now)
    if window.start or window.end:
        day, first = posted.local_date(), posted.earliest_date()
        if window.end and first > window.end:
            return "out", f"posted {shown}: after your date range ({window.label()})"
        if window.start and day < window.start:
            return "out", f"posted {shown}: before your date range ({window.label()})"
        if posted.open_ended:
            return "unknown", f"posted {shown}: it may also be older than {window.start} - yours to decide"
        if (window.end and day > window.end) or (window.start and first < window.start):
            return "unknown", (f"posted {shown}: the listing's label is precise only to {_precision(posted.spread_s)}, "
                               f"so it may lie outside {window.label()} - yours to decide")
        return "in", f"posted {shown}: within {window.label()}"
    assert window.days is not None
    if posted.date_only:  # a calendar date: "the last 7 days" are today and the 7 days before it
        inside = age_days(posted, now=now) <= window.days
    else:
        inside = posted.at >= now - timedelta(days=window.days)
    if not inside:
        return "out", f"posted {shown}: outside your job-recency window ({window.label()})"
    if posted.open_ended:
        return "unknown", f"posted {shown}: possibly much earlier - outside your window ({window.label()}) is likely"
    if not posted.date_only and posted.earliest < now - timedelta(days=window.days):
        return "unknown", (f"posted {shown}: the listing's label is precise only to {_precision(posted.spread_s)}, "
                           f"so it may be just outside your window ({window.label()}) - yours to decide")
    return "in", f"posted {shown}: within {window.label()}"


def age_days(posted: Posted, *, now: datetime | None = None) -> float:
    """How many days ago it was posted: by the clock, or by calendar days for a date without a time."""
    now = now or utcnow()
    if posted.date_only:
        return float((now.replace(tzinfo=UTC).astimezone(local_tz()).date() - posted.local_date()).days)
    return (now - posted.at).total_seconds() / 86400


def describe(posted: Posted | None, *, now: datetime | None = None) -> str:
    """'on 2026-06-25 (111 days ago, the portal's date)', 'at least 23 days ago (on or before 2026-09-21)'..."""
    if posted is None:
        return "unknown"
    now = now or utcnow()
    days = age_days(posted, now=now)
    when = posted.local_date().isoformat()
    if days < 1:
        hours = max(int(days * 24), 0)
        age = "today" if posted.date_only else ("less than an hour ago" if hours == 0 else
                                                f"{hours} hour{'s' if hours != 1 else ''} ago")
    else:
        age = f"{int(days)} day{'s' if int(days) != 1 else ''} ago"
    basis = "the portal's date" if posted.source == "page" else "from the listing's label"
    if posted.open_ended:
        return f"at least {age} (on or before {when}, {basis})"
    return f"on {when} ({age}, {basis})"


# ---------------------------------------------------------------- reading it off a job record


def listing_closed(job: Any, *, now: datetime | None = None) -> str:
    """'' or why the listing is closed whatever its age: its own end date ("validThrough") has passed."""
    end = getattr(job, "valid_through", None)
    now = now or utcnow()
    if end is not None and end < now:
        return f"the listing's own end date ({end.date().isoformat()}) has passed"
    return ""


def posted_of(job: Any) -> Posted | None:
    """The posting date the record holds: the stored one, else the listing label read when the job was found (the
    earliest moment it was read - so an estimate errs towards "older", never "newer")."""
    at = getattr(job, "posted_at", None)
    if at is not None:
        source = getattr(job, "posted_source", "") or "label"
        spread = getattr(job, "posted_spread_s", None)
        if spread is None and source == "label":  # recorded before the precision was kept: the label's own unit
            spread = label_spread(getattr(job, "posted_text", "") or "")
        return Posted(at, open_ended=bool(getattr(job, "posted_open_ended", False)),
                      date_only=bool(getattr(job, "posted_date_only", False)), source=source, spread_s=int(spread or 0))
    text = getattr(job, "posted_text", "") or ""
    seen = getattr(job, "discovered_at", None)
    return parse_label(text, seen=seen) if text and seen else None


def describe_posted(job: Any, *, now: datetime | None = None) -> str:
    posted = posted_of(job)
    if posted is None:
        return "unknown"
    raw = (getattr(job, "posted_text", "") or "").strip()
    note = f" - '{raw}' when found on {job.discovered_at.date().isoformat()}" if raw and posted.source == "label" \
        and getattr(job, "discovered_at", None) else ""
    return describe(posted, now=now) + note
