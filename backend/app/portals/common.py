"""Helpers shared by portal adapters."""

from __future__ import annotations

import re
from typing import Any

from playwright.async_api import Error as PlaywrightError, Page

SUCCESS_TEXT = re.compile(
    r"successfully\s+applied|applied\s+successfully|application\s+(?:has\s+been\s+|was\s+)?(?:sent|submitted|received)|"
    r"you\s+(?:have\s+)?applied|thank\s+you\s+for\s+(?:applying|your\s+application|submitting)|"
    r"your\s+application\s+(?:has\s+been|was)\s+(?:sent|submitted|received)|we(?:'ve|\s+have)\s+received\s+your\s+application",
    re.I)

EXPIRED_TEXT = re.compile(r"no\s+longer\s+(?:available|accepting)|job\s+(?:has\s+)?expired|this\s+job\s+is\s+closed|"
                          r"position\s+has\s+been\s+filled|not\s+accepting\s+applications", re.I)


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
