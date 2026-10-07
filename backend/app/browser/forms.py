"""Generic form automation: discover labelled fields on any page and fill them from the Answerer.

Field discovery runs a single JS pass that understands <label for>, wrapping labels, aria-labelledby,
fieldset legends, nearby question text, radio/checkbox groups, selects, comboboxes, contenteditable
and open shadow DOM. Every field gets a ``data-pja-id`` so it can be located reliably afterwards.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.async_api import Error as PlaywrightError, Frame, Locator, Page

from app.answers.answerer import Answerer
from app.answers.options import choose_option, real_options
from app.answers.question_bank import Answer, QuestionContext
from app.browser.engine import BrowserEngine
from app.matching.analyzer import JobInput

log = logging.getLogger(__name__)

Root = Page | Frame | Locator

_SCAN_JS = r"""
(container) => {
  const doc = document;
  const base = container || doc;
  const T = (el) => el ? (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim() : '';
  const shown = (el) => {
    if (!el || !el.isConnected || el.nodeType !== 1) return false;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const roots = [];
  const addRoot = (r) => { roots.push(r); r.querySelectorAll('*').forEach(e => { if (e.shadowRoot) addRoot(e.shadowRoot); }); };
  addRoot(base);
  if (base !== doc && base.shadowRoot) addRoot(base.shadowRoot);
  const all = [];
  for (const r of roots) r.querySelectorAll('input, textarea, select, [contenteditable="true"], [role="combobox"]:not(input)').forEach(e => all.push(e));

  const byId = (el, id) => { const rn = el.getRootNode(); return (rn.getElementById ? rn.getElementById(id) : null) || doc.getElementById(id); };
  const labelEl = (el) => {
    if (el.id) { try { const l = el.getRootNode().querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) return l; } catch (e) {} }
    return el.closest('label');
  };
  const ownLabel = (el) => {
    const lb = el.getAttribute('aria-labelledby');
    if (lb) { const t = lb.split(/\s+/).map(id => T(byId(el, id))).join(' ').trim(); if (t) return t; }
    const l = labelEl(el); if (l && T(l)) return T(l);
    const al = el.getAttribute('aria-label'); if (al && al.trim()) return al.trim();
    return '';
  };
  const STRICT = 'label, legend, [class*="label" i], [class*="question" i], [data-automation-id*="label" i], [data-testid*="label" i], [data-testid*="question" i]';
  const LOOSE = 'h2, h3, h4, h5, h6, p, span, strong, div';
  const contextLabel = (el, exclude) => {
    let node = el.parentElement;
    for (let depth = 0; depth < 6 && node && node !== doc.body; depth++, node = node.parentElement) {
      for (const sel of [STRICT, LOOSE]) {
        const cands = Array.from(node.querySelectorAll(sel)).filter(c =>
          !c.contains(el) && !(exclude || []).some(x => c.contains(x) || x.contains(c)) &&
          (c.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) &&
          !c.querySelector('input, select, textarea') && T(c) && T(c).length <= 300);
        if (cands.length) return T(cands[cands.length - 1]);
      }
    }
    return '';
  };
  const humanize = (s) => (s || '').replace(/([a-z])([A-Z])/g, '$1 $2').replace(/[_\-\[\]\.]+/g, ' ').trim();
  const tag = (el) => { if (!el.dataset.pjaId) { window.__pjaSeq = (window.__pjaSeq || 0) + 1; el.dataset.pjaId = 'pja-' + window.__pjaSeq; } return el.dataset.pjaId; };
  const requiredOf = (el, label, lEl) => !!(el.required || el.getAttribute('aria-required') === 'true' ||
      /[*✱]/.test(label || '') || (lEl && /required/i.test(lEl.className || '')));

  const out = []; const groups = new Map();
  for (const el of all) {
    const tn = el.tagName.toLowerCase();
    let type = tn === 'input' ? (el.getAttribute('type') || 'text').toLowerCase() : tn;
    if (['hidden', 'submit', 'button', 'image', 'reset'].includes(type)) continue;
    if (el.disabled) continue;
    if (el.getAttribute('contenteditable') === 'true') type = 'contenteditable';
    else if (el.getAttribute('role') === 'combobox' && tn !== 'select') type = 'combobox';
    if (type === 'search' && el.getAttribute('role') !== 'combobox') continue;
    const lEl = labelEl(el);
    const visible = shown(el) || (['radio', 'checkbox', 'file'].includes(type) && (shown(lEl) || shown(el.parentElement)));
    if (!visible && type !== 'file') continue;
    if (type === 'radio' || type === 'checkbox') {
      const sameName = el.name ? Array.from(el.getRootNode().querySelectorAll('input[type="' + type + '"]')).filter(x => x.name === el.name) : [el];
      if (type === 'radio' || sameName.length > 1) {
        const key = type + ':' + (el.name || tag(el));
        let g = groups.get(key);
        if (!g) {
          const container = el.closest('fieldset, [role="radiogroup"], [role="group"]');
          let q = '';
          if (container) {
            const lg = container.querySelector('legend');
            const lb = container.getAttribute('aria-labelledby');
            q = (lg && T(lg)) || (lb ? lb.split(/\s+/).map(id => T(byId(el, id))).join(' ').trim() : '') || container.getAttribute('aria-label') || '';
          }
          if (!q) q = contextLabel(el, sameName.map(x => labelEl(x)).filter(Boolean).concat(sameName));
          g = { id: tag(el), type: type === 'radio' ? 'radio' : 'checkbox_group', label: q || humanize(el.name),
                name: el.name || '', required: false, options: [], value: '', maxLength: null, placeholder: '', accept: '', multiple: type === 'checkbox' };
          groups.set(key, g); out.push(g);
        }
        const optLabel = ownLabel(el) || el.value || '';
        g.options.push({ label: optLabel, id: tag(el), checked: !!el.checked });
        if (el.checked) g.value = g.value ? g.value + ', ' + optLabel : optLabel;
        g.required = g.required || requiredOf(el, g.label, null);
        continue;
      }
    }
    let label = ownLabel(el);
    if (!label || (type === 'checkbox' && label.length < 3)) label = label || contextLabel(el, lEl ? [lEl] : []);
    if (!label) label = el.getAttribute('placeholder') || humanize(el.getAttribute('name') || el.id || '');
    const f = { id: tag(el), type, label, name: el.getAttribute('name') || '', required: requiredOf(el, label, lEl),
                options: [], value: '', maxLength: el.maxLength > 0 ? el.maxLength : null,
                placeholder: el.getAttribute('placeholder') || '', accept: el.getAttribute('accept') || '', multiple: !!el.multiple };
    if (type === 'select') {
      f.options = Array.from(el.options).map(o => ({ label: (o.text || '').trim(), id: o.value, checked: o.selected }));
      const sel = el.options[el.selectedIndex];
      f.value = sel && sel.value !== '' && el.selectedIndex >= 0 && !/^\s*(select|choose|--|please)/i.test(sel.text) ? sel.text.trim() : '';
    } else if (type === 'checkbox') {
      f.value = el.checked ? 'true' : '';
    } else if (type === 'contenteditable') {
      f.value = T(el);
    } else if (type === 'file') {
      f.value = el.files && el.files.length ? el.files[0].name : '';
    } else {
      f.value = el.value || '';
    }
    out.push(f);
  }
  return out;
}
"""

_ERRORS_JS = r"""
(container) => {
  const base = container || document;
  const T = (el) => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  const shown = (el) => { const r = el.getBoundingClientRect(); const cs = getComputedStyle(el); return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none'; };
  const sels = '[role="alert"], .error, .errors, .field-error, .error-message, .invalid-feedback, [class*="error" i]:not(input):not(form):not(body):not(html), [class*="invalid" i]:not(input):not(form), [data-automation-id*="error" i], .artdeco-inline-feedback--error';
  const seen = new Set(); const out = [];
  base.querySelectorAll(sels).forEach(el => { if (!shown(el)) return; const t = T(el); if (t && t.length < 300 && !seen.has(t)) { seen.add(t); out.push(t); } });
  return out.slice(0, 12);
}
"""

COVER_RX = re.compile(r"cover\s*letter", re.I)


@dataclass
class FormOption:
    label: str
    id: str
    checked: bool = False


@dataclass
class FormField:
    id: str
    type: str
    label: str
    name: str = ""
    required: bool = False
    options: list[FormOption] = field(default_factory=list)
    value: str = ""
    max_length: int | None = None
    placeholder: str = ""
    accept: str = ""
    multiple: bool = False

    @property
    def selector(self) -> str:
        return f'[data-pja-id="{self.id}"]'

    @property
    def option_labels(self) -> list[str]:
        return [o.label for o in self.options]

    @property
    def is_empty(self) -> bool:
        return not (self.value or "").strip()

    @classmethod
    def from_js(cls, d: dict[str, Any]) -> FormField:
        return cls(id=d["id"], type=d["type"], label=re.sub(r"\s+", " ", d.get("label") or "").strip(),
                   name=d.get("name") or "", required=bool(d.get("required")),
                   options=[FormOption(o.get("label") or "", str(o.get("id") or ""), bool(o.get("checked")))
                            for o in d.get("options") or []],
                   value=d.get("value") or "", max_length=d.get("maxLength"), placeholder=d.get("placeholder") or "",
                   accept=d.get("accept") or "", multiple=bool(d.get("multiple")))


@dataclass
class FilledField:
    field: FormField
    question: str
    answer: Answer | None
    status: str  # filled|prefilled|unresolved|skipped|uploaded|failed
    note: str = ""


@dataclass
class FillReport:
    items: list[FilledField] = field(default_factory=list)
    resume_uploaded: bool = False

    @property
    def unresolved(self) -> list[FilledField]:
        return [i for i in self.items if i.status in {"unresolved", "failed"} and i.field.required]

    @property
    def filled(self) -> list[FilledField]:
        return [i for i in self.items if i.status in {"filled", "uploaded"}]


AnswerCallback = Callable[[FormField, QuestionContext, Answer], Awaitable[None] | None]


def _clean_question(label: str) -> str:
    q = re.sub(r"\s*[*✱]\s*", " ", label).strip()
    q = re.sub(r"\(\s*(?:required|optional)\s*\)", "", q, flags=re.I).strip()
    return re.sub(r"\s+", " ", q)


async def scan_fields(root: Root) -> list[FormField]:
    try:
        if isinstance(root, Locator):
            raw = await root.evaluate(_SCAN_JS)
        else:
            raw = await root.evaluate(_SCAN_JS, None)
    except PlaywrightError as exc:
        log.warning("Field scan failed: %s", str(exc).splitlines()[0])
        return []
    return [FormField.from_js(d) for d in raw or []]


async def collect_errors(root: Root) -> list[str]:
    try:
        if isinstance(root, Locator):
            return await root.evaluate(_ERRORS_JS)
        return await root.evaluate(_ERRORS_JS, None)
    except PlaywrightError:
        return []


class FormFiller:
    """Fill every visible field in ``root`` using the Answerer. Records each Q/A via ``on_answer``."""

    def mark_resume_handled(self) -> None:
        self._handled.add("resume")

    def __init__(self, engine: BrowserEngine, answerer: Answerer, *, resume_path: str | Path | None,
                 job: JobInput | None, portal: str = "", on_answer: AnswerCallback | None = None,
                 fill_uncertain: bool = True) -> None:
        self.engine = engine
        self.answerer = answerer
        self.resume_path = Path(resume_path) if resume_path else None
        self.job = job
        self.portal = portal
        self.on_answer = on_answer
        self.fill_uncertain = fill_uncertain  # fill low-confidence suggestions (never submitted without review)
        self._handled: set[str] = set()

    async def fill(self, root: Root, *, overwrite_prefilled: bool = False, skip: set[str] | None = None) -> FillReport:
        report = FillReport()
        fields = await scan_fields(root)
        for f in fields:
            if f.id in (skip or set()):
                continue
            await self.engine.checkpoint()
            try:
                item = await self._fill_one(root, f, overwrite_prefilled)
            except Exception as exc:
                log.warning("Filling '%s' failed: %s", f.label, exc)
                item = FilledField(f, f.label, None, "failed", str(exc).splitlines()[0][:200])
            report.items.append(item)
            if item.status == "uploaded":
                report.resume_uploaded = True
        return report

    async def _fill_one(self, root: Root, f: FormField, overwrite_prefilled: bool) -> FilledField:
        question = _clean_question(f.label) or f.name or f.placeholder
        loc = _root_locator(root, f.selector)

        if f.type == "file":
            return await self._fill_file(loc, f, question)

        prefilled = not f.is_empty and f.type not in {"checkbox"}
        if prefilled and not overwrite_prefilled:
            return FilledField(f, question, None, "prefilled", f"kept existing value '{f.value[:60]}'")
        if not question:
            return FilledField(f, question, None, "skipped" if not f.required else "unresolved",
                               "field has no label")

        ftype = {"contenteditable": "textarea", "combobox": "select" if f.options else "text",
                 "checkbox_group": "checkbox_group"}.get(f.type, f.type)
        ctx = QuestionContext(question=question, field_type=ftype, options=f.option_labels,
                              required=f.required, max_length=f.max_length, job=self.job, portal=self.portal)
        ans = await self.answerer.answer(ctx)
        if self.on_answer is not None:
            res = self.on_answer(f, ctx, ans)
            if hasattr(res, "__await__"):
                await res  # type: ignore[misc]

        if ans.value is None or (ans.value == "" and f.required):
            return FilledField(f, question, ans, "unresolved" if f.required else "skipped", ans.note)
        if ans.value == "":
            return FilledField(f, question, ans, "skipped", ans.note)
        if ans.needs_review and not (self.fill_uncertain and f.required):
            return FilledField(f, question, ans, "unresolved" if f.required else "skipped", ans.note)

        ok = await self.set_value(loc, f, ans.value)
        if not ok:
            return FilledField(f, question, ans, "failed", f"could not set value '{ans.value[:60]}'")
        status = "unresolved" if ans.needs_review else "filled"
        return FilledField(f, question, ans, status, ans.note)

    async def _fill_file(self, loc: Locator, f: FormField, question: str) -> FilledField:
        label = f"{f.label} {f.name} {f.accept}"
        if COVER_RX.search(label):
            return FilledField(f, question or "Cover letter", None, "unresolved" if f.required else "skipped",
                               "no cover-letter file configured")
        if not f.is_empty:
            return FilledField(f, question or "Resume", None, "prefilled", f"file already attached: {f.value}")
        if "resume" in self._handled:
            return FilledField(f, question or "Resume", None, "prefilled", "resume already provided")
        # the first non-cover-letter file input is treated as the resume upload
        if self.resume_path is None or not self.resume_path.is_file():
            return FilledField(f, question or "Resume", None, "unresolved", "no resume file available")
        await loc.set_input_files(str(self.resume_path))
        self._handled.add("resume")
        await self.engine.human_delay(500, 1200)
        return FilledField(f, question or "Resume", None, "uploaded", self.resume_path.name)

    async def set_value(self, loc: Locator, f: FormField, value: str) -> bool:
        try:
            if f.type in {"text", "email", "tel", "url", "number", "textarea", "password", "date", "month",
                          "datetime-local", "time", "week"}:
                if f.type == "number":
                    m = re.search(r"-?\d+(?:\.\d+)?", value.replace(",", ""))
                    if not m:
                        return False
                    value = m.group(0)
                await loc.fill(value)
                current = await loc.input_value()
                if current.strip() != value.strip():
                    await loc.fill("")
                    await loc.press_sequentially(value, delay=15)
                await self.engine.human_delay(60, 200)
                return True
            if f.type == "contenteditable":
                await loc.click()
                await loc.press("ControlOrMeta+a")
                await loc.press_sequentially(value, delay=10)
                return True
            if f.type == "select":
                try:
                    await loc.select_option(label=value, timeout=4000)
                except PlaywrightError:
                    match = next((o for o in f.options if o.label.strip().lower() == value.strip().lower()), None)
                    if match is None:
                        return False
                    await loc.select_option(value=match.id, timeout=4000)
                return True
            if f.type == "radio":
                opt = next((o for o in f.options if o.label.strip().lower() == value.strip().lower()), None)
                if opt is None:
                    picked = choose_option(value, f.option_labels)
                    opt = next((o for o in f.options if o.label == picked), None)
                if opt is None:
                    return False
                return await _check(loc.page, opt.id)
            if f.type == "checkbox":
                want = value.strip().lower() in {"yes", "true", "y", "1", "i agree", "agree", "checked"}
                page = loc.page
                is_checked = await loc.is_checked()
                if want != is_checked:
                    return await _check(page, f.id, uncheck=not want)
                return True
            if f.type == "checkbox_group":
                wanted = [v.strip() for v in re.split(r"[,;]\s*", value) if v.strip()]
                ok_any = False
                for w in wanted:
                    picked = choose_option(w, f.option_labels)
                    opt = next((o for o in f.options if o.label == picked), None)
                    if opt is not None:
                        ok_any = await _check(loc.page, opt.id) or ok_any
                return ok_any
            if f.type == "combobox":
                return await self._fill_combobox(loc, value)
        except PlaywrightError as exc:
            log.debug("set_value failed for %s: %s", f.label, str(exc).splitlines()[0])
            return False
        return False

    async def _fill_combobox(self, loc: Locator, value: str) -> bool:
        """Typeahead / react-select: type, wait for options, pick the best one."""
        page = loc.page
        await loc.click()
        try:
            await loc.fill("")
            await loc.press_sequentially(value[:40], delay=40)
        except PlaywrightError:
            await page.keyboard.type(value[:40], delay=40)
        await page.wait_for_timeout(700)
        options = page.locator("[role='option']:visible, [role='listbox'] li:visible, .select__option:visible")
        count = await options.count()
        texts = [((await options.nth(i).inner_text()) or "").strip() for i in range(min(count, 30))]
        picked = choose_option(value, texts, min_score=75) if texts else None
        if picked is not None:
            await options.nth(texts.index(picked)).click()
            return True
        if count:
            await page.keyboard.press("Enter")
            return True
        return False


def _root_locator(root: Root, selector: str) -> Locator:
    return root.locator(selector).first


async def _check(page: Page, pja_id: str, uncheck: bool = False) -> bool:
    loc = page.locator(f'[data-pja-id="{pja_id}"]').first
    try:
        if uncheck:
            await loc.uncheck(timeout=3000)
        else:
            await loc.check(timeout=3000)
        return True
    except PlaywrightError:
        pass
    # custom-styled inputs are often visually hidden: click the label instead
    try:
        return bool(await loc.evaluate(
            """(el, uncheck) => {
                 if (el.checked === !uncheck) return true;
                 const lbl = (el.id && el.getRootNode().querySelector('label[for="' + CSS.escape(el.id) + '"]')) || el.closest('label');
                 (lbl || el).click();
                 return el.checked === !uncheck;
               }""", uncheck))
    except PlaywrightError:
        return False


def summarize_unresolved(report: FillReport) -> list[str]:
    return [f"{i.question or i.field.name}: {i.note}" for i in report.unresolved]


__all__ = ["FormField", "FormFiller", "FillReport", "FilledField", "scan_fields", "collect_errors",
           "summarize_unresolved", "real_options"]
