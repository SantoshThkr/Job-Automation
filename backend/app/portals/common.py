"""Helpers shared by portal adapters."""

from __future__ import annotations

import re
from typing import Any

from playwright.async_api import Error as PlaywrightError, Page

SUCCESS_TEXT = re.compile(
    r"successfully\s+applied|applied\s+successfully|application\s+(?:has\s+been\s+|was\s+)?(?:sent|submitted|received)|"
    r"you\s+(?:have\s+)?applied|thank\s+you\s+for\s+(?:applying|your\s+application|submitting)|thanks\s+for\s+applying|"
    r"your\s+application\s+(?:has\s+been|was|is)\s+(?:successfully\s+)?(?:sent|submitted|received|complete)|"
    r"we(?:'ve|\s+have)\s+(?:successfully\s+)?received\s+your\s+(?:application|resume|cv|profile)|"
    r"application\s+(?:has\s+been\s+)?completed\b|submission\s+(?:was\s+)?successful|"
    r"successfully\s+submitted\s+your\s+application|your\s+(?:resume|cv|profile)\s+(?:has\s+been|was)\s+"
    r"(?:successfully\s+)?(?:sent|submitted|received)|"
    r"(?:the\s+|your\s+)?(?:form|details)\s+(?:was\s+|were\s+|has\s+been\s+|have\s+been\s+)?(?:sent|submitted)\s+successfully",
    re.I)

# the site failed while receiving the form: the application may or may not have arrived
SERVER_ERROR_TEXT = re.compile(r"^\s*(?:5\d\d\s+[a-z ]{3,40}|bad\s+gateway|service\s+(?:temporarily\s+)?unavailable|"
                               r"internal\s+server\s+error|gateway\s+time-?out)\b", re.I)

EXPIRED_TEXT = re.compile(
    r"no\s+longer\s+(?:available|accepting|active|open)|job\s+(?:has\s+|is\s+)?expired|this\s+job\s+is\s+(?:closed|no\s+longer)|"
    r"(?:position|job|role|vacancy|requisition|opening)\s+(?:has\s+been\s+|is\s+|was\s+)?(?:filled|closed)\b|"
    r"not\s+accepting\s+applications|job\s+you\s+are\s+looking\s+for\s+(?:is\s+|has\s+)?(?:expired|closed|not\s+available|"
    r"no\s+longer|been\s+removed)|posting\s+(?:has\s+)?(?:expired|closed|been\s+removed)|applications?\s+(?:are\s+|is\s+)?closed|"
    r"job\s+(?:posting\s+)?(?:not\s+found|does\s+not\s+exist|is\s+unavailable)", re.I)


def slugify(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", (text or "").lower())).strip("-")


async def extract_cards(page: Page, card_selectors: list[str], fields: dict[str, list[str]],
                        multi: dict[str, list[str]] | None = None, attrs: dict[str, tuple[list[str], str]] | None = None,
                        flags: dict[str, list[str]] | None = None) -> list[dict[str, Any]]:
    """Extract listing cards in one JS pass using fallback selector lists for every field."""
    js = r"""
    (a) => {
      let cards = [];
      for (const s of a.cards) { try { cards = Array.from(document.querySelectorAll(s)); } catch (e) { cards = []; } if (cards.length) break; }
      const T = (el) => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
      const pick = (root, sels) => { for (const s of sels) { try { const el = root.querySelector(s); if (el && T(el)) return T(el); } catch (e) {} } return ''; };
      const pickAll = (root, sels) => { for (const s of sels) { try { const els = Array.from(root.querySelectorAll(s)).map(T).filter(Boolean); if (els.length) return els; } catch (e) {} } return []; };
      const pickAttr = (root, sels, attr) => { for (const s of sels) { try { const el = s === ':self' ? root : root.querySelector(s); const v = el && el.getAttribute(attr); if (v) return v; } catch (e) {} } return ''; };
      const has = (root, sels) => sels.some(s => { try { return root.matches(s) || !!root.querySelector(s); } catch (e) { return false; } });
      return cards.map(c => {
        const o = {};
        for (const [k, sels] of Object.entries(a.fields)) o[k] = pick(c, sels);
        for (const [k, sels] of Object.entries(a.multi)) o[k] = pickAll(c, sels);
        for (const [k, spec] of Object.entries(a.attrs)) o[k] = pickAttr(c, spec[0], spec[1]);
        for (const [k, sels] of Object.entries(a.flags)) o[k] = has(c, sels);
        o._text = T(c).slice(0, 600);
        return o;
      });
    }
    """
    try:
        return await page.evaluate(js, {"cards": card_selectors, "fields": fields, "multi": multi or {},
                                        "attrs": attrs or {}, "flags": flags or {}})
    except PlaywrightError:
        return []


_LD_DATES_JS = r"""() => {
  const out = {};
  for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
    const t = s.textContent || '';
    if (!/JobPosting/i.test(t)) continue;  // only the job's own structured data, never a widget's
    for (const k of ['datePosted', 'validThrough']) {
      // (a regex literal: in a string, '\s' would be a plain 's' - and pretty-printed JSON, "datePosted": "...", missed)
      const m = t.match(k === 'datePosted' ? /"datePosted"\s*:\s*"([^"]{8,40})"/ : /"validThrough"\s*:\s*"([^"]{8,40})"/);
      if (m && !out[k]) out[k] = m[1];
    }
  }
  return out;
}"""


async def read_page_dates(page: Page, job) -> None:
    """The portal's own posting date and end date from the job page's structured data (schema.org JobPosting), when
    it has them - the reliable evidence (Naukri's "3+ weeks ago" was, on one page, 2026-06-25)."""
    from app.matching.posting import parse_date
    from app.matching.posting import utcnow as now_utc

    try:
        found = await page.evaluate(_LD_DATES_JS)
    except Exception:  # noqa: BLE001 - optional evidence
        return
    if posted := parse_date(str(found.get("datePosted") or ""), now=now_utc()):
        job.posted_at, job.posted_date_only = posted.at, posted.date_only
    if end := parse_date(str(found.get("validThrough") or ""), now=now_utc().replace(year=now_utc().year + 5)):
        job.valid_through = end.at


async def first_text(page: Page, selectors: list[str], *, max_len: int = 20_000) -> str:
    js = r"""
    (sels) => { for (const s of sels) { try { const el = document.querySelector(s);
      if (el) { const t = (el.innerText || el.textContent || '').trim(); if (t) return t; } } catch (e) {} } return ''; }
    """
    try:
        return (await page.evaluate(js, selectors))[:max_len]
    except PlaywrightError:
        return ""


async def all_texts(page: Page, selectors: list[str]) -> list[str]:
    js = r"""
    (sels) => { for (const s of sels) { try { const els = Array.from(document.querySelectorAll(s))
      .map(e => (e.innerText || e.textContent || '').replace(/\s+/g, ' ').trim()).filter(Boolean);
      if (els.length) return els; } catch (e) {} } return []; }
    """
    try:
        return await page.evaluate(js, selectors)
    except PlaywrightError:
        return []


async def page_has_success(page: Page) -> str:
    try:
        body = await page.evaluate("() => (document.body && document.body.innerText || '').slice(0, 8000)")
    except PlaywrightError:
        return ""
    m = SUCCESS_TEXT.search(body or "")
    return m.group(0) if m else ""


async def generic_apply_button(page: Page):
    """Find the job page's Apply button by what it says (not by markup) when a portal's selectors fail.

    Returns ("external" | "apply", locator) or None. "external" = an "Apply on company site" style button.
    """
    from app.navigator.classify import Kind
    from app.navigator.observe import observe

    try:
        snap = await observe(page)
    except PlaywrightError:
        return None
    cands = [e for e in snap.by_kind(Kind.APPLY) if not e.ctx]
    if not cands:
        return None
    best = sorted(cands, key=lambda e: (0 if e.tag in {"button", "input"} else 1, e.y))[0]
    loc = snap.frame(best.frame).locator(best.selector).first
    kind = "external" if re.search(r"company|employer|corporate", best.label, re.I) else "apply"
    return kind, loc
