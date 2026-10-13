"""Local-LLM fallback for pages the rules cannot read.

The model never writes selectors or free-form actions: it picks one element id from the list of
clickable elements the observer found (or says it needs the user / cannot continue). The
navigator validates the choice and still routes every submit-like click through the submit gate.
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import urlparse
from dataclasses import dataclass

from app.ai.provider import AIResponseError, AIUnavailable, OllamaProvider, get_provider
from app.navigator.classify import Kind
from app.navigator.observe import Element, Snapshot

log = logging.getLogger(__name__)

PAGE_TYPES = ["job_description", "application_form", "login_or_signup", "confirmation", "already_applied",
              "closed_or_expired", "error", "popup_or_overlay", "unrelated", "other"]

_SCHEMA = {
    "type": "object",
    "properties": {
        "page_type": {"type": "string", "enum": PAGE_TYPES},
        "action": {"type": "string", "enum": ["click", "wait_for_user", "stop"]},
        "element_id": {"type": "string"},
        "reason": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["page_type", "action", "element_id", "reason", "confidence"],
}

_SYSTEM = (
    "You operate a web browser to complete ONE job application for a candidate. You see a summary of the "
    "current page and a numbered list of clickable elements. Pick the single next click that moves THIS job "
    "application forward. Answer with JSON only.")

_RULES = """Rules:
- Good choices: Apply / Apply now / Start application / I'm interested / Continue / Next / Apply as guest / Apply without account / closing a popup that blocks the page.
- Never choose: sign in with Google/LinkedIn/Facebook/Apple, logout, share, save job, job alerts, newsletter, social media, cookie settings, language switch, unrelated navigation (About, Blog, Products, Investors, Contact).
- If the page needs the person (password login, CAPTCHA, OTP, email verification), use action "wait_for_user".
- If the job is closed, the page is unrelated, or nothing can move the application forward, use action "stop".
- element_id must be copied exactly from the list (like "a12"), or be "" when action is not "click"."""


@dataclass
class PlannerDecision:
    page_type: str
    action: str
    element: Element | None
    reason: str
    confidence: float


def _site(url: str) -> str:
    host = urlparse(url).netloc.lower().split(":")[0]
    parts = host.split(".")
    return ".".join(parts[-3:] if len(parts) > 2 and len(parts[-1]) == 2 and parts[-2] in {"co", "com", "org", "net", "ac", "gov"}
                    else parts[-2:])


def candidate_elements(snap: Snapshot, limit: int = 40, exclude: set[str] | None = None) -> list[Element]:
    """Elements worth showing to the model: no dangerous / SSO / disabled items, main content first."""
    exclude = exclude or set()
    useful_nav = re.compile(r"apply|career|job|position|opening|vacanc|continue|next|start|guest", re.I)
    out: list[Element] = []
    for e in snap.elements:
        if e.id in exclude or e.disabled or e.kind in {Kind.DANGER, Kind.SSO}:
            continue
        if not e.label or len(e.label) > 80:
            continue
        if e.ctx and not useful_nav.search(e.label):
            continue
        out.append(e)
    if any(e.dialog and e.kind not in {Kind.CLOSE, Kind.COOKIES} for e in out):
        out = [e for e in out if e.dialog]  # an open dialog blocks the page behind it
    here = _site(snap.url)
    out = [e for e in out if not (e.tag == "a" and e.href.startswith("http") and _site(e.href) != here
                                  and not useful_nav.search(e.label))]  # links to other sites are reading material

    # dialogs first (they block everything else), then apply-ish, then top-to-bottom
    def rank(e: Element) -> tuple:
        applyish = 0 if e.kind in {Kind.APPLY, Kind.NEXT, Kind.SUBMIT, Kind.GUEST, Kind.CLOSE} else 1
        return (0 if e.dialog else 1, applyish, e.y)
    return sorted(out, key=rank)[:limit]


def build_prompt(snap: Snapshot, goal: str, history: list[str], candidates: list[Element]) -> str:
    fields = [f.field.label[:60] for f in snap.app_fields][:15]
    text = re.sub(r"\n{2,}", "\n", snap.text or "")[:1800]
    lines = [
        f"GOAL: {goal}",
        "",
        f"PAGE URL: {snap.url}",
        f"PAGE TITLE: {snap.title}",
        f"HEADINGS: {' | '.join(snap.headings[:8])}",
        f"OPEN DIALOGS: {len(snap.dialogs)}" + (f" (first: {snap.dialogs[0][:200]!r})" if snap.dialogs else ""),
        f"FORM FIELDS ({len(snap.app_fields)}): {', '.join(fields) or 'none'}",
        f"PASSWORD FIELDS: {snap.password_count}",
        "",
        "VISIBLE TEXT (excerpt):",
        text,
        "",
        "CLICKABLE ELEMENTS:",
        *[e.describe() for e in candidates],
        "",
        "RECENT ACTIONS: " + ("; ".join(history[-6:]) or "none"),
        "",
        _RULES,
        "",
        'Return JSON: {"page_type": one of ' + json.dumps(PAGE_TYPES) + ', "action": "click"|"wait_for_user"|"stop", '
        '"element_id": "...", "reason": "short", "confidence": 0.0-1.0}',
    ]
    return "\n".join(lines)


class PageReasoner:
    def __init__(self, provider: OllamaProvider | None = None, *, min_confidence: float = 0.45) -> None:
        self.provider = provider or get_provider()
        self.min_confidence = min_confidence

    async def available(self) -> bool:
        try:
            return await self.provider.is_available()
        except Exception:
            return False

    async def decide(self, snap: Snapshot, goal: str, history: list[str],
                     exclude: set[str] | None = None) -> PlannerDecision | None:
        candidates = candidate_elements(snap, exclude=exclude)
        if not candidates and not snap.text:
            return None
        prompt = build_prompt(snap, goal, history, candidates)
        try:
            data = await self.provider.chat_json(_SYSTEM, prompt, json_schema=_SCHEMA, max_tokens=220, temperature=0.0)
        except (AIUnavailable, AIResponseError) as exc:
            log.info("Planner unavailable: %s", exc)
            return None
        if not isinstance(data, dict):
            return None
        action = str(data.get("action") or "").strip().lower()
        page_type = str(data.get("page_type") or "other").strip().lower()
        reason = str(data.get("reason") or "")[:300]
        try:
            conf = float(data.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        conf = conf / 100 if conf > 1 else conf
        element = None
        if action == "click":
            raw_id = str(data.get("element_id") or "").strip().strip("[]")
            element = next((e for e in candidates if e.id == raw_id), None)
            if element is None:  # the model invented or mistyped an id: not trusted
                log.info("Planner chose unknown element %r", raw_id)
                return PlannerDecision(page_type, "invalid", None, f"unknown element {raw_id!r}", 0.0)
            if conf < self.min_confidence:
                return PlannerDecision(page_type, "low_confidence", element, reason, conf)
        elif action not in {"wait_for_user", "stop"}:
            return None
        return PlannerDecision(page_type, action, element, reason, conf)
