"""Compile profile queries for each portal's search field."""

import re

from app.matching.analyzer import classify_title

_SKILL_TERMS = [(r"next\.?js", "Next.js"), ("typescript", "TypeScript"), ("javascript", "JavaScript"), ("react", "React")]


_FULLSTACK = re.compile(r"\b(?:full[ -]?stack|mern)\b", re.I)
_OFF_TARGET = re.compile(r"\b(?:management|manager|sales|account(?:ant|ing| executive)|graphic|designer|"
                         r"recruiter|marketing|director|internship|mobile|native|devops|tester|qa)\b", re.I)


def frontend_queries(portal: str, queries: list[str]) -> list[str]:
    result: dict[str, str] = {}
    for raw in queries:
        title = " ".join(raw.split())
        engineering = re.search(r"\b(?:engineers?|developers?|engineering|development)\b", title, re.I)
        skill_only = re.fullmatch(r"(?:full[ -]?stack(?: ai)?|mern|front[ -]?end|react(?:\.?js)?|next\.?js|"
                                  r"javascript|typescript)", title, re.I)
        if _OFF_TARGET.search(title) or not (engineering or skill_only):
            continue
        if not _FULLSTACK.search(title) and not re.search(r"front[ -]?end|\b(?:ui|react(?:\.?js)?|javascript|typescript)\b|next\.?js", title, re.I):
            continue
        if classify_title(title) in {"devops", "mobile", "data", "qa", "backend"}:
            continue
        if portal == "instahyre" and not _FULLSTACK.search(title):
            title = next((skill for pattern, skill in _SKILL_TERMS if re.search(pattern, title, re.I)), "JavaScript")
        elif _FULLSTACK.search(title) and portal == "instahyre":
            title = ("MERN" if re.search(r"\bmern\b", title, re.I) else "Full Stack AI"
                     if re.search(r"\bai\b", title, re.I) else "Full Stack FastAPI" if "fastapi" in title.casefold()
                     else "Full Stack Python" if "python" in title.casefold() else "Full Stack React")
        result.setdefault(title.casefold(), title)
    return list(result.values())
