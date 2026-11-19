"""Compile profile queries for each portal's search field."""

import re

from app.matching.analyzer import classify_title

_SKILL_TERMS = [(r"next\.?js", "Next.js"), ("typescript", "TypeScript"), ("javascript", "JavaScript"), ("react", "React")]


def frontend_queries(portal: str, queries: list[str]) -> list[str]:
    result: dict[str, str] = {}
    for raw in queries:
        title = " ".join(raw.split())
        if not re.search(r"front[ -]?end|\b(?:ui|react|javascript|typescript)\b|next\.?js", title, re.I):
            continue
        if classify_title(title) in {"devops", "mobile", "data", "qa", "backend"}:
            continue
        if portal == "instahyre":
            title = next((skill for pattern, skill in _SKILL_TERMS if re.search(pattern, title, re.I)), "JavaScript")
        elif re.search(r"front[ -]?end (?:platform|ai) engineer", title, re.I):
            title = "Frontend Engineer"
        result.setdefault(title.casefold(), title)
    return list(result.values())
