"""Generic form automation: discover labelled fields on any page and fill them from the Answerer.

Field discovery runs a single JS pass that understands <label for>, wrapping labels, aria-labelledby,
fieldset legends, nearby question text, radio/checkbox groups, selects, comboboxes, contenteditable
and open shadow DOM. Every field gets a ``data-pja-id`` so it can be located reliably afterwards.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Frame, Locator, Page

from app.answers.answerer import Answerer
from app.answers.normalize import NUMBER, fit_number, refit_after_rejection
from app.answers.options import choose_option, real_options, split_choices
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
  for (const r of roots) r.querySelectorAll('input, textarea, select, [contenteditable="true"], [role="combobox"]:not(input), button[aria-haspopup="listbox"]').forEach(e => all.push(e));

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
  // a <label for=X> belongs to another field only when X really is another control (sloppy pages point 'for' at
  // ids that do not exist - those labels still describe the field right after them)
  const labelsOther = (lbl) => { const t = byId(lbl, lbl.htmlFor); return !!t && /^(INPUT|SELECT|TEXTAREA)$/.test(t.tagName); };
  // ...unless the 'for' is a copy-paste slip: the field it names has its own earlier label, and this label sits
  // alone with our field in one box (<label for="name">Contact Number</label><input id="contact">)
  const strayFor = (lbl, el) => { try {
    const first = lbl.getRootNode().querySelector('label[for="' + CSS.escape(lbl.htmlFor) + '"]'); const box = lbl.parentElement;
    return !!first && first !== lbl && !!box && box.contains(el) && box.querySelectorAll('input, select, textarea').length === 1;
  } catch (e) { return false; } };
  const STRICT = 'label, legend, [class*="label" i], [class*="question" i], [data-automation-id*="label" i], [data-testid*="label" i], [data-testid*="question" i]';
  const LOOSE = 'h2, h3, h4, h5, h6, p, span, strong, div';
  const contextLabel = (el, exclude) => {
    let node = el.parentElement;
    for (let depth = 0; depth < 6 && node && node !== doc.body; depth++, node = node.parentElement) {
      for (const sel of [STRICT, LOOSE]) {
        const cands = Array.from(node.querySelectorAll(sel)).filter(c =>
          !c.contains(el) && !(exclude || []).some(x => c.contains(x) || x.contains(c)) &&
          !(c.tagName === 'LABEL' && c.htmlFor && c.htmlFor !== el.id && labelsOther(c) && !strayFor(c, el)) &&
          (c.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) &&
          !c.querySelector('input, select, textarea') && /\p{L}{2}/u.test(T(c)) && T(c).length <= 300);  // not '×'
        if (cands.length) return T(cands[cands.length - 1]);
      }
    }
    return '';
  };
  const GENERIC = /^(?:[*✱]+|type\s+(?:your\s+|an?\s+)?(?:answer|response|text)(?:\s+here)?|enter\s+(?:your\s+|the\s+)?(?:answer|response|url|link|value|text|here)(?:\s+here)?|your\s+(?:answer|response)|answer|response|select\.{0,3}|choose\.{0,3}|select\s+an?\s+option|\d+\s*digits?(?:\s+only)?|years?|yrs?\.?|months?|mos?\.?|optional|required|.{0,40}\bis\s+required\.?)$/i;
  const UNIT = /^(?:(years?|yrs?)|(months?|mos?))\.?$/i;
  const lastPart = (s) => { const parts = (s || '').split(/[\[\]\.]+/).filter(Boolean); return parts.length ? parts[parts.length - 1] : (s || ''); };
  const ABBR = { mob: 'mobile', mobno: 'mobile number', phno: 'phone number', no: 'number', exp: 'experience', expr: 'experience',
                 qual: 'qualification', addr: 'address', dob: 'date of birth', yrs: 'years', loc: 'location', curr: 'current',
                 ctc: 'ctc', ectc: 'expected ctc', cctc: 'current ctc', np: 'notice period', fname: 'first name', lname: 'last name' };
  const expand = (s) => (s || '').split(/\s+/).map(w => ABBR[w.toLowerCase()] || w).join(' ');
  const humanize = (s) => (s || '').replace(/([a-z])([A-Z])/g, '$1 $2').replace(/[_\-\[\]\.]+/g, ' ').trim();
  const tag = (el) => { if (!el.dataset.pjaId) { window.__pjaSeq = (window.__pjaSeq || 0) + 1; el.dataset.pjaId = 'pja-' + window.__pjaSeq; } return el.dataset.pjaId; };
  const wrapperRequired = (el) => { let n = el.parentElement;
    for (let d = 0; d < 3 && n && n.tagName !== 'FORM' && n !== doc.body; d++, n = n.parentElement) {
      if (/(^|[\s_-])(is-)?required($|[\s_-])/i.test((n.className || '').toString()) && n.querySelectorAll('input, select, textarea').length <= 3) return true;
    } return false; };
  const requiredOf = (el, label, lEl) => !!(el.required || el.getAttribute('aria-required') === 'true' ||
      /[*✱]/.test(label || '') || (lEl && /required/i.test(lEl.className || '')) || wrapperRequired(el));

  // A web component often holds only the control: its question is an attribute of the component, a label
  // pointing at it, text placed inside it, or the label just before it - all outside the control's shadow root
  // (SmartRecruiters' screening questions were read as "question bfc6ed48 ..." and its consent box not at all).
  const hostOf = (el) => { const rn = el.getRootNode(); return rn && rn.host ? rn.host : null; };
  const UNIT_TEXT = /^(?:[A-Z]{3}|[$€£₹%]|years?|yrs?|months?|days?|lpa|lakhs?|per\s+annum|\d+\s*\/\s*\d+)\.?$/i;
  const hostLabel = (el) => {
    let h = hostOf(el);
    for (let d = 0; d < 4 && h; d++, h = hostOf(h)) {
      const named = h.getAttribute('label') || h.getAttribute('data-label');
      if (named && named.trim()) return named.trim();
      // what the page shows inside the component is the question; an aria-label is often only a description of
      // the box ("Salary in INR" on the box under "What is your current compensation?")
      const inside = T(h);
      if (inside && /\p{L}{2}/u.test(inside) && inside.length <= 300 && !UNIT_TEXT.test(inside) &&
          !h.querySelector('input, select, textarea')) return inside;
      const aria = h.getAttribute('aria-labelledby') ? '' : (h.getAttribute('aria-label') || '');
      const own = ownLabel(h) || aria.trim();
      if (own && !UNIT_TEXT.test(own)) return own;
      const around = contextLabel(h, []);
      // (a currency or unit next to the box - "INR", "%", "years" - is not its question: look one component further out)
      if (around && !GENERIC.test(around.trim()) && !UNIT_TEXT.test(around.trim())) return around;
    }
    return '';
  };

  const out = []; const groups = new Map();
  for (const el of all) {
    const tn = el.tagName.toLowerCase();
    let type = tn === 'input' ? (el.getAttribute('type') || 'text').toLowerCase() : tn;
    if (tn === 'button' && el.getAttribute('aria-haspopup') === 'listbox') type = 'listbox_button';
    if (['hidden', 'submit', 'button', 'image', 'reset'].includes(type)) continue;
    if (el.disabled) continue;
    if (el.getAttribute('contenteditable') === 'true') type = 'contenteditable';
    else if (type === 'listbox_button' || (el.getAttribute('role') === 'combobox' && tn !== 'select')) type = 'combobox';
    // the search box of a dropdown widget without ARIA (Semantic UI ".ui.dropdown input.search", Select2, Chosen):
    // what is typed there is only a filter - the value is the list item that gets chosen
    else if (tn === 'input' && ['text', 'search'].includes(type) &&
             el.closest('.ui.dropdown, .select2-container, .chosen-container, [class*="search-dropdown" i]') &&
             /(^|\s)(search|select2-search__field|chosen-search-input)(\s|$)/.test(el.className || '')) type = 'combobox';
    if (type === 'search' && el.getAttribute('role') !== 'combobox') continue;
    const lEl = labelEl(el);
    const visible = shown(el) || (['radio', 'checkbox', 'file'].includes(type) &&
                                  (shown(lEl) || shown(el.parentElement) || shown(hostOf(el))));
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
        let optLabel = ownLabel(el);
        if (!optLabel) {  // card-style options: the text of the box that holds only this radio
          let box = el.parentElement;
          for (let d = 0; d < 4 && box && !optLabel; d++, box = box.parentElement) {
            if (box.querySelectorAll('input[type="' + type + '"]').length !== 1) break;
            const rr = box.querySelector('[role="radio"][aria-label], [role="checkbox"][aria-label]');
            optLabel = (rr && rr.getAttribute('aria-label')) || (T(box).length <= 120 ? T(box) : '');
          }
        }
        // an explicit value="..." can stand in for a missing label; the DOM default ("on") says nothing
        const rawValue = el.getAttribute('value');
        optLabel = optLabel || (rawValue && rawValue !== 'on' ? rawValue : '');
        g.options.push({ label: optLabel, id: tag(el), checked: !!el.checked });
        if (el.checked) g.value = g.value ? g.value + ', ' + optLabel : optLabel;
        g.required = g.required || requiredOf(el, g.label, null);
        continue;
      }
    }
    let label = ownLabel(el);
    // the search box of a dropdown widget: the widget is the control (its entries and its shown value are not labels)
    const widget = type === 'combobox' && tn === 'input' ? el.closest('.ui.dropdown, .select2-container, .chosen-container') : null;
    const native = widget ? widget.querySelector('select') : null;
    if (!label && widget) label = (native && ownLabel(native)) || contextLabel(widget, []) || '';
    const borrowed = !label;
    // (the component's own label may hold nothing but the "*" of a required box, the sentence being slotted in)
    const weak = !label || GENERIC.test(label.trim());
    const fromHost = weak && hostOf(el) ? hostLabel(el) : '';
    if (fromHost && label && weak) label = fromHost + (/[*✱]/.test(label) && !/[*✱]/.test(fromHost) ? ' *' : '');
    if (type === 'checkbox' && !label && !fromHost && el.getAttribute('tabindex') === '-1' && !el.required) continue;  // widget internals
    if (!label || (type === 'checkbox' && label.length < 3)) label = label || fromHost || contextLabel(el, lEl ? [lEl] : []);
    if (label && GENERIC.test(label.trim())) {  // "Type your answer here", "*", "years": the question is nearby
      const around = contextLabel(el, lEl ? [lEl] : []);
      if (around && !GENERIC.test(around.trim())) {
        const u = label.trim().match(UNIT);
        const req = /[*✱]/.test(label);
        label = around + (u ? (u[1] ? ' (whole years)' : ' (additional months)') : '') + (req && !/[*✱]/.test(around) ? ' *' : '');
      }
    }
    if (!label) label = el.getAttribute('placeholder') || expand(humanize(lastPart(el.getAttribute('name') || el.id || '')));
    const req = borrowed && type === 'checkbox' ? !!(el.required || el.getAttribute('aria-required') === 'true')
                                                : requiredOf(el, label, lEl);
    const f = { id: tag(el), type, label, name: el.getAttribute('name') || '', required: req, ownLabel: !borrowed,
                options: [], value: '', maxLength: el.maxLength > 0 ? el.maxLength : null,
                placeholder: el.getAttribute('placeholder') || '', accept: el.getAttribute('accept') || '', multiple: !!el.multiple,
                step: el.getAttribute('step') || '', min: el.getAttribute('min') || '', max: el.getAttribute('max') || '',
                pattern: el.getAttribute('pattern') || '', inputmode: el.getAttribute('inputmode') || '' };
    if (type === 'select') {
      f.options = Array.from(el.options).map(o => ({ label: (o.text || '').trim(), id: o.value, checked: o.selected }));
      const sel = el.options[el.selectedIndex];
      // nothing chosen yet: the browser shows the first option, and the form would submit it as the answer
      const first = el.options[0];
      f.defaulted = el.selectedIndex === 0 && el.options.length > 1 && !first.defaultSelected && !el.dataset.pjaSet &&
                    first.value !== '' && !/^\s*(select|choose|--|please|none\s+selected)/i.test(first.text || '');
      f.value = !f.defaulted && sel && sel.value !== '' && el.selectedIndex >= 0 && !/^\s*(select|choose|--|please)/i.test(sel.text) ? sel.text.trim() : '';
    } else if (type === 'checkbox') {
      f.value = el.checked ? 'true' : '';
    } else if (type === 'contenteditable') {
      f.value = T(el);
    } else if (type === 'combobox' && tn === 'input' && el.closest('.ui.dropdown, .select2-container, .chosen-container')) {
      // a dropdown widget's search box is empty even when something is chosen: the choice is shown beside it
      const wrap = el.closest('.ui.dropdown, .select2-container, .chosen-container');
      const tags = Array.from(wrap.querySelectorAll('a.ui.label, .select2-selection__choice, .search-choice')).map(T).filter(Boolean);
      const one = wrap.querySelector('.text:not(.default), .select2-selection__rendered, .chosen-single span');
      f.value = tags.length ? tags.join(', ') : ((one && !/^(select|choose|search|--|please)/i.test(T(one)) ? T(one) : '') || el.value || '');
      f.multiple = /(^|\s)multiple(\s|$)/.test(wrap.className || '') || !!wrap.querySelector('select[multiple]');
      f.widget = true;
      // the entries it offers are those of the native <select> it wraps (a list loaded while typing has none yet)
      const sel = wrap.querySelector('select');
      if (sel) f.options = Array.from(sel.options).filter(o => o.value !== '').slice(0, 1500)
                                .map(o => ({ label: (o.text || '').trim(), id: o.value, checked: o.selected }));
    } else if (type === 'combobox' && tn !== 'input') {
      // custom dropdown (button / div): its text is the current choice unless it is a prompt like "Select One"
      const shownText = T(el);
      f.value = /^(select|choose|--|please|none selected)/i.test(shownText) ? '' : shownText;
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
  // (jQuery Validate reports in <label class="error" for="message">: that label is the message, not a question label)
  const inQuestionLabel = (x) => { const l = x.closest('label'); return !!l && !/(^|[\s_-])(error|invalid)/i.test(l.className || ''); };
  // messages inside web components (open shadow roots) count too: a component often reports its own field
  const roots = [base];
  const walk = (root, depth) => { if (depth > 4) return; root.querySelectorAll('*').forEach(el => {
    if (el.shadowRoot && roots.length < 200) { roots.push(el.shadowRoot); walk(el.shadowRoot, depth + 1); } }); };
  walk(base, 0);
  roots.forEach(root => root.querySelectorAll(sels).forEach(el => { if (!shown(el) || inQuestionLabel(el)) return; const t = T(el);
    if (/^[\s*✱:]*$|^\(?\s*required\s*\)?$/i.test(t)) return;  // required markers, not messages
    if (t && t.length < 300 && !seen.has(t)) { seen.add(t); out.push(t); } }));
  return out.slice(0, 12);
}
"""

COVER_RX = re.compile(r"cover\s*letter", re.I)
RESUME_RX = re.compile(r"resume|r\u00e9sum\u00e9|\bcv\b|curriculum", re.I)
# uploads that are not for a document: your résumé is never offered as a picture
PICTURE_RX = re.compile(r"photo|picture|avatar|profile\s*(?:image|pic)|head\s*shot|selfie|\blogo\b|\bimage\b|"
                        r"passport[\s-]size", re.I)
# uploads that ask for some other document (never satisfied with the résumé a second time)
OTHER_DOCUMENT_RX = re.compile(r"id\s+proof|aadha?ar|\bpan\b|passport|certificate|transcript|mark\s*sheet|offer\s+letter|"
                               r"pay\s*slip|salary\s+slip|experience\s+letter|relieving|portfolio\s+file|work\s+sample", re.I)
_IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".jfif", ".svg", ".heic", ".avif"}
# a site's own refusal of what was just uploaded ("this file format is not supported ... .BMP,.GIF,.JPEG,.PNG")
_FORMAT_REFUSED = re.compile(r"(?:file\s+)?(?:format|type|extension)\s+(?:is\s+)?(?:not\s+(?:supported|allowed|valid|"
                             r"accepted)|invalid|unsupported)|(?:unsupported|invalid)\s+file|only\s+(?:image|jpe?g|png)", re.I)


def images_only(accept: str) -> bool:
    """A file input's ``accept`` that allows nothing but pictures (image/*, .png, .jpg ...)."""
    kinds = [k.strip().lower() for k in (accept or "").split(",") if k.strip()]
    return bool(kinds) and all(k.startswith("image/") or k in _IMAGE_TYPES for k in kinds)


async def _mark_chosen(loc: Locator) -> None:
    """A <select> the assistant set (even to its first option) is answered, not a browser default, on the next scan."""
    try:
        await loc.evaluate("e => { e.dataset.pjaSet = '1'; }")
    except PlaywrightError:
        pass


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
    step: str = ""  # numeric representation hints read from the DOM (see answers/normalize.py)
    min: str = ""
    max: str = ""
    pattern: str = ""
    inputmode: str = ""
    defaulted: bool = False  # a <select> still on the browser's first option: nobody chose it, but it would be sent
    own_label: bool = False  # the label is the field's own (<label for>, wrapping label, aria-label), not nearby text
    widget: bool = False  # the search box of a dropdown widget: what is typed only filters, a list entry must be chosen

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
                   name=d.get("name") or "", required=bool(d.get("required") or d.get("defaulted")),
                   options=[FormOption(o.get("label") or "", str(o.get("id") or ""), bool(o.get("checked")))
                            for o in d.get("options") or []],
                   value=d.get("value") or "", max_length=d.get("maxLength"), placeholder=d.get("placeholder") or "",
                   accept=d.get("accept") or "", multiple=bool(d.get("multiple")), step=str(d.get("step") or ""),
                   min=str(d.get("min") or ""), max=str(d.get("max") or ""), pattern=str(d.get("pattern") or ""),
                   inputmode=str(d.get("inputmode") or ""), defaulted=bool(d.get("defaulted")),
                   own_label=bool(d.get("ownLabel")), widget=bool(d.get("widget")))


@dataclass
class FilledField:
    field: FormField
    question: str
    answer: Answer | None
    status: str  # filled|prefilled|unresolved|skipped|uploaded|failed
    note: str = ""
    verified: bool | None = None  # the control shows the value we set and reports no validation error


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
ResultCallback = Callable[["FilledField"], Awaitable[None] | None]

_VERIFY_JS = r"""
(el) => {
  let got = '';
  if (el.tagName === 'SELECT') got = el.selectedIndex >= 0 ? (el.options[el.selectedIndex].text || '') : '';
  else if (el.isContentEditable) got = el.innerText || '';
  else if (el.type === 'checkbox' || el.type === 'radio') got = el.checked ? '__checked__' : '';
  else if (el.tagName === 'BUTTON' || !('value' in el)) got = el.innerText || el.textContent || '';
  else got = el.value || '';
  const widget = el.tagName === 'INPUT' ? el.closest('.ui.dropdown, .select2-container, .chosen-container') : null;
  if (widget) {  // (never the text left in its filter box: that is not a choice)
    const tags = Array.from(widget.querySelectorAll('a.ui.label, .select2-selection__choice, .search-choice')).map(t => (t.textContent || '').trim());
    const one = widget.querySelector('.text:not(.default), .select2-selection__rendered, .chosen-single span');
    got = tags.length ? tags.join(', ') : (one ? (one.textContent || '').trim() : '');
  }
  let err = '';
  const inQuestionLabel = (x) => { const l = x.closest('label'); return !!l && !/(^|[\s_-])(error|invalid)/i.test(l.className || ''); };
  let wrap = el.parentElement;
  for (let d = 0; d < 4 && wrap && wrap !== document.body; d++, wrap = wrap.parentElement) {
    if (wrap.querySelectorAll('input:not([type=hidden]), select, textarea').length > 4) break;
    // a required-marker '*' styled with an "error" class (WP Job Openings: span.awsm-job-form-error) or anything
    // inside the field's own label is not a validation message
    const e = Array.from(wrap.querySelectorAll('[role="alert"], .error, .error-message, .field-error, .invalid-feedback, [class*="error" i]:not(input):not(select):not(textarea):not(form)'))
      .find(x => x.getClientRects().length && !inQuestionLabel(x) && !/^[\s*✱:]*$|^\(?\s*required\s*\)?$/i.test((x.innerText || '').trim()));
    if (e) { err = e.innerText.trim().slice(0, 160); break; }
  }
  if (!err) {  // messages linked to the field itself (LinkedIn 2026: hashed classes, no "error" class names)
    const ids = ((el.getAttribute('aria-errormessage') || '') + ' ' + (el.getAttribute('aria-describedby') || '')).split(/\s+/).filter(Boolean);
    for (const id of ids) {
      const node = document.getElementById(id);
      const t = node && node.getClientRects().length ? (node.innerText || '').trim() : '';
      if (t && (el.getAttribute('aria-invalid') === 'true' || /required|invalid|enter\s+a|must|whole number|decimal|between|valid/i.test(t))) { err = t.slice(0, 160); break; }
    }
  }
  const v = el.validity;  // the browser's own constraints: pattern, type=email/url, min/max, length
  const native = !!(v && !v.valid && (v.patternMismatch || v.typeMismatch || v.badInput || v.tooLong || v.tooShort ||
                                      v.rangeOverflow || v.rangeUnderflow || v.stepMismatch));
  if (native && !err) err = (el.validationMessage || 'value does not match the expected format').slice(0, 160);
  return { got: got.trim(), invalid: el.getAttribute('aria-invalid') === 'true' || native, err };
}
"""


def _same(got: str, want: str) -> bool:
    g = re.sub(r"\s+", " ", got or "").strip().lower()
    w = re.sub(r"\s+", " ", want or "").strip().lower()
    if not w:
        return True
    if g == w or (g and (g.startswith(w[: max(1, len(g) - 1)]) if len(g) < len(w) else w in g)):
        return True
    gd, wd = re.sub(r"\D", "", g), re.sub(r"\D", "", w)
    return bool(wd) and len(wd) >= 4 and (gd.endswith(wd) or wd.endswith(gd)) and len(gd) >= 4


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
    return fix_shared_labels([FormField.from_js(d) for d in raw or []])


def _prompt_option(f: FormField) -> str:
    """A select's first option often carries the label ("Total Experience*", "Select notice period")."""
    if f.type != "select" or not f.options:
        return ""
    first = f.options[0]
    text = first.label.strip()
    if not text or (first.id and not re.match(r"^\s*(?:select|choose|--)", text, re.I)):
        return ""
    return re.sub(r"^\s*(?:-+\s*)?(?:please\s+)?(?:select|choose)\s+(?:your\s+|an?\s+|the\s+)?|\s*-+\s*$", "", text,
                  flags=re.I).strip()


def _humanize(name: str) -> str:
    n = re.sub(r"^(?:z|txt|inp|input|fld|field)(?=[a-z_])", "", name or "")
    n = re.sub(r"([a-z])([A-Z])", r"\1 \2", n)
    return re.sub(r"[_\-\[\]\.]+", " ", n).strip()


def fix_shared_labels(fields: list[FormField]) -> list[FormField]:
    """Placeholder-labelled forms: when several fields inherited the same nearby heading ("APPLY HERE",
    "Personal details") as their label, use each field's own placeholder / prompt option / name instead.
    A field's own label is never replaced: two forms on one page may each have a "Full Name" box, and their
    placeholders are examples ("Alex Morgan"), not questions."""
    from collections import Counter

    counts = Counter(f.label for f in fields if f.label and f.type not in {"radio", "checkbox_group", "checkbox"})
    for f in fields:
        if not f.label or counts[f.label] < 2 or f.type in {"radio", "checkbox_group", "checkbox", "file"} or f.own_label:
            continue
        alt = f.placeholder.strip() or _prompt_option(f) or _humanize(f.name)
        if alt:
            if "*" in alt:
                f.required = True
            f.label = alt
    return fields


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
                 fill_uncertain: bool = True, on_result: ResultCallback | None = None) -> None:
        self.engine = engine
        self.answerer = answerer
        self.resume_path = Path(resume_path) if resume_path else None
        self.job = job
        self.portal = portal
        self.on_answer = on_answer
        self.on_result = on_result
        self.fill_uncertain = fill_uncertain  # fill low-confidence suggestions (never submitted without review)
        self.page_context = ""  # visible page text: company context for written answers
        self._handled: set[str] = set()
        self._repaired: dict[str, set[str]] = {}
        self._not_documents: set[str] = set()  # file inputs that refused a document (picture uploads)
        self._resume_inputs: set[str] = set()  # the uploads labelled for a résumé, when a form has several
        self._file_attached = False  # an upload box of the form being filled holds a file right now
        self._repaired_invalid: set[str] = set()

    async def fill(self, root: Root, *, overwrite_prefilled: bool = False, skip: set[str] | None = None) -> FillReport:
        report = FillReport()
        fields = await scan_fields(root)
        # several uploads on one form (a picture, a résumé, other documents): the one labelled for a résumé gets it
        uploads = [f for f in fields if f.type == "file" and f.id not in (skip or set())]
        self._file_attached = any(not f.is_empty for f in uploads)  # some upload box of this form holds a file now
        named = {f.id for f in uploads if RESUME_RX.search(f"{f.label} {f.name}") and not COVER_RX.search(f.label)}
        self._resume_inputs = named if len(uploads) > 1 else set()
        section: dict[str, str] = {}  # Company / Title of the repeating entry (e.g. one work experience) we are in
        for f in fields:
            if f.id in (skip or set()):
                _note_section(section, f.label, f.value)
                continue
            await self.engine.checkpoint()
            try:
                item = await self._fill_one(root, f, overwrite_prefilled, section=dict(section))
            except Exception as exc:
                log.warning("Filling '%s' failed: %s", f.label, exc)
                item = FilledField(f, f.label, None, "failed", str(exc).splitlines()[0][:200])
            report.items.append(item)
            _note_section(section, f.label, f.value if item.status == "prefilled" or item.answer is None
                          else (item.answer.value or ""))
            if item.status == "uploaded":
                report.resume_uploaded = True
            if self.on_result is not None and item.answer is not None:
                res = self.on_result(item)
                if hasattr(res, "__await__"):
                    await res  # type: ignore[misc]
        return report

    async def _fill_one(self, root: Root, f: FormField, overwrite_prefilled: bool, *,
                        section: dict[str, str] | None = None) -> FilledField:
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

        if f.type == "combobox" and not f.options:
            # custom dropdown: open it to see the real choices, so the answer is one of them
            f.options = [FormOption(t, "") for t in await self._peek_options(loc)]
        ftype = {"contenteditable": "textarea", "combobox": ("checkbox_group" if f.multiple else "select") if f.options else "text",
                 "checkbox_group": "checkbox_group"}.get(f.type, f.type)
        ctx = QuestionContext(question=question, field_type=ftype, options=f.option_labels,
                              required=f.required, max_length=f.max_length, job=self.job, portal=self.portal,
                              page_context=self.page_context, section=section or {})
        ans = await self.answerer.answer(ctx)
        if f.type in {"email", "tel"} and not ans.value:  # label unreadable ("+358" dial code): the box type decides
            by_type = self._value_for_type(f)
            if by_type:
                ans.value, ans.confidence, ans.needs_review, ans.source = by_type, 0.95, False, "profile"
                ans.mode, ans.note = "profile", f"{f.type} box (label '{f.label[:30]}' unreadable): your {f.type}"
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

        if f.type in {"number", "text", "tel"}:
            fitted = fit_number(ans.value, field_type=f.type, step=f.step, min_=f.min, max_=f.max, pattern=f.pattern,
                                inputmode=f.inputmode, hint=f"{f.label} {f.placeholder}")
            if fitted.value is None:
                ans.needs_review = True
                return FilledField(f, question, ans, "unresolved" if f.required else "skipped", fitted.note)
            if fitted.changed:
                ans.note = f"{ans.note}; {fitted.note}".strip("; ")
                ans.value = fitted.value
        mismatch = _type_mismatch(f, ans.value)
        if mismatch:  # the control's own type says the label was misread (an email box labelled "Name")
            by_type = self._value_for_type(f)
            if by_type is None:
                ans.needs_review = True
                return FilledField(f, question, ans, "unresolved" if f.required else "skipped", mismatch)
            ans.value, ans.note = by_type, f"{f.type} box (its label '{f.label[:40]}' did not match): entered your {f.type}"
            ans.needs_review = False
        ok = await self.set_value(loc, f, ans.value, root=root)
        verified, problem = (await self.verify(loc, f, ans.value, root)) if ok else (False, "")
        if ok and not verified:
            # the control did not take it: try another way (typing, label click, fuzzy option) and check again
            if await self._retry_set(loc, f, ans.value, root):
                verified, problem = await self.verify(loc, f, ans.value, root)
        if ok and not verified and problem and NUMBER.match(ans.value or ""):
            refit = refit_after_rejection(ans.value, problem, field_type=f.type, step=f.step, min_=f.min, max_=f.max,
                                          pattern=f.pattern, inputmode=f.inputmode, hint=f"{f.label} {f.placeholder}")
            if refit.value is not None and await self.set_value(loc, f, refit.value, root=root):
                verified, problem = await self.verify(loc, f, refit.value, root)
                ans.note = f"{ans.note}; {refit.note}".strip("; ")
                ans.value = refit.value
        if ok and not verified and problem:
            for alt in self.alternate_formats(f, question, ans.value):  # same fact, the format the site expects
                if await self.set_value(loc, f, alt, root=root):
                    verified, problem = await self.verify(loc, f, alt, root)
                    if verified:
                        ans.note = f"{ans.note}; site expects '{alt}'".strip("; ")
                        ans.value = alt
                        break
        ans.verified = verified if ok else False
        if not ok:
            return FilledField(f, question, ans, "failed", f"could not set value '{ans.value[:60]}'", verified=False)
        if not verified and problem:
            return FilledField(f, question, ans, "failed", f"the form did not accept '{ans.value[:60]}': {problem}",
                               verified=False)
        status = "unresolved" if ans.needs_review else "filled"
        return FilledField(f, question, ans, status, ans.note, verified=verified)

    def _value_for_type(self, f: FormField) -> str | None:
        personal = getattr(getattr(self.answerer, "profile", None), "personal", None)
        if personal is None:
            return None
        if f.type == "email":
            return personal.email or None
        if f.type == "tel":
            national = getattr(personal, "phone_national", "") or personal.phone or ""
            cc = (getattr(personal, "phone_country_code", "") or "").strip()
            dial_widget = bool(re.fullmatch(r"\+\d{1,4}", (f.label or "").strip())) or "intl" in (f.name or "").lower()
            if national and cc and dial_widget:  # a country dial-code picker next to the box: say which country
                return f"{cc if cc.startswith('+') else '+' + cc} {national}"
            return national or None
        return None

    async def fix_invalid(self, root: Root) -> InvalidReport:
        """Fields the page marks invalid after Next / Submit (aria-invalid, linked error text, native validity):
        one informed repair each - pick the autocomplete suggestion, or refit a number to what the field accepts.
        Returns what was fixed and the messages that remain (for you)."""
        out = InvalidReport()
        try:
            bad = await (root.evaluate(_INVALID_JS) if isinstance(root, Locator) else root.evaluate(_INVALID_JS, None))
        except PlaywrightError:
            return out
        fields = {f.id: f for f in await scan_fields(root)}
        for b in bad:
            f = fields.get(b.get("id") or "")
            msg = (b.get("message") or "").strip()
            label = (f.label if f else b.get("label")) or "field"
            out.messages.append(f"{label}: {msg}" if msg else f"{label}: marked invalid")
            if f is None or f.id in self._repaired_invalid:
                continue
            self._repaired_invalid.add(f.id)
            loc = _root_locator(root, f.selector)
            value = (f.value or "").strip()
            if f.type in {"text", "number", "tel"} and NUMBER.match(value):
                refit = refit_after_rejection(value, msg, field_type=f.type, step=f.step, min_=f.min, max_=f.max,
                                              pattern=f.pattern, inputmode=f.inputmode, hint=f"{f.label} {f.placeholder}")
                if refit.value is not None and await self.set_value(loc, f, refit.value, root=root):
                    out.fixed.append(f"{label} = {refit.value} ({refit.note})")
                continue
            if f.type in {"text", "search"} and value:
                await loc.fill("")
                if await self.set_value(loc, f, value, root=root) and (await loc.input_value()).strip() != value:
                    out.fixed.append(f"{label} = {(await loc.input_value()).strip()} (picked from the suggestions)")
        return out

    _PHONE_LABEL = re.compile(r"phone|mobile|contact\s+(?:number|no)|whatsapp|\bcell\b", re.I)

    def alternate_formats(self, f: FormField, question: str, value: str) -> list[str]:
        """Other ways of writing the same verified value (never a different fact)."""
        if f.type not in {"text", "tel", "number"} or not self._PHONE_LABEL.search(question or f.label):
            return []
        digits = re.sub(r"\D", "", value or "")
        prof = getattr(self.answerer, "profile", None)
        cc = re.sub(r"\D", "", getattr(getattr(prof, "personal", None), "phone_country_code", "") or "")
        national = digits[len(cc):] if cc and digits.startswith(cc) and len(digits) > 10 else digits
        out = [national, f"+{cc}{national}" if cc else "", f"{cc}{national}" if cc else "", f"+{cc} {national}" if cc else ""]
        return [x for x in dict.fromkeys(out) if x and x != value]

    _TOO_LONG = re.compile(r"(?:more|longer|greater)\s+than\s+(\d{2,5})\s+char|(?:max(?:imum)?|limit(?:ed)?|up)\s+(?:length\s+)?"
                           r"(?:of\s+|to\s+|is\s+)?(\d{2,5})\s+char|(\d{2,5})\s+characters?\s+(?:max|or\s+(?:less|fewer)|"
                           r"limit|allowed)|exceeds?\s+(?:the\s+)?(?:max\w*\s+)?(?:length|limit)\s+of\s+(\d{2,5})", re.I)

    async def _repair_length(self, root: Root, errors: list[str]) -> bool:
        """'Message can't be more than 500 characters': the same text, cut at a sentence end inside the limit."""
        limits = [(int(next(g for g in m.groups() if g)), e) for e in errors if (m := self._TOO_LONG.search(e))]
        if not limits:
            return False
        changed = False
        for f in await scan_fields(root):
            if f.type not in {"text", "textarea", "contenteditable"} or f.id in self._repaired_invalid:
                continue
            fits = [n for n, e in limits if len(f.value or "") > n]
            if not fits:
                continue
            named = [n for n, e in limits if len(f.value) > n and (f.label or "\0").strip(" *:").lower()[:20] in e.lower()]
            limit = min(named or fits)
            cut = f.value[:limit]
            end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
            shorter = cut[: end + 1] if end >= limit * 0.5 else cut.rsplit(" ", 1)[0]
            self._repaired_invalid.add(f.id)
            if shorter.strip() and await self.set_value(_root_locator(root, f.selector), f, shorter.strip(), root=root):
                changed = True
        return changed

    _RESUME_MISSING = re.compile(r"(?:attach|upload|select|choose|provide|add)\w*\s+(?:you\w*\s+|a\s+|the\s+)?(?:resume|cv)\b|"
                                 r"(?:resume|cv)(?:\s*/\s*(?:resume|cv))?\s+(?:is\s+)?(?:required|missing|mandatory)", re.I)

    async def _repair_resume(self, root: Root, errors: list[str]) -> bool:
        """'Please attach your resume' although it was attached: the site dropped its own copy (it uploads the file
        when it is chosen and forgot it when the form was refused and redrawn). The file is chosen again, once."""
        if not any(self._RESUME_MISSING.search(e) for e in errors):
            return False
        if self.resume_path is None or not self.resume_path.is_file() or "resume-again" in self._handled:
            return False
        uploads = [f for f in await scan_fields(root) if f.type == "file" and f.id not in self._not_documents
                   and not images_only(f.accept) and not COVER_RX.search(f.label)
                   and not OTHER_DOCUMENT_RX.search(f"{f.label} {f.name}")]
        target = next((f for f in uploads if RESUME_RX.search(f"{f.label} {f.name}")), uploads[0] if uploads else None)
        if target is None:
            return False
        loc = _root_locator(root, target.selector)
        try:
            await loc.set_input_files([])
            await self._attach(loc, self.resume_path, again=True)
        except PlaywrightError as exc:
            log.debug("could not attach the resume again: %s", exc)
            return False
        self._handled.update({"resume", "resume-again"})
        await self.engine.human_delay(1200, 2000)  # the site's own upload of the chosen file
        return True

    async def repair(self, root: Root, errors: list[str]) -> bool:
        """The site rejected a value after Next/Submit ("Please enter a valid phone number", "Message can't be more
        than 500 characters"): refill the field with the same fact in the form the site accepts. Returns True when
        something was changed (the caller retries once)."""
        if await self._repair_length(root, errors):
            return True
        if await self._repair_resume(root, errors):
            return True
        if not any(self._PHONE_LABEL.search(e) for e in errors):
            return False
        changed = False
        for f in await scan_fields(root):
            if f.type not in {"text", "tel", "number"} or not self._PHONE_LABEL.search(f.label) or f.is_empty:
                continue
            if re.search(r"country\s*code|dial", f.label, re.I):
                continue
            tried = self._repaired.setdefault(f.id, {f.value})
            alts = [a for a in self.alternate_formats(f, f.label, f.value) if a not in tried]
            if alts and await self.set_value(_root_locator(root, f.selector), f, alts[0], root=root):
                tried.add(alts[0])
                changed = True
        return changed

    async def verify(self, loc: Locator, f: FormField, value: str, root: Root | None = None) -> tuple[bool, str]:
        """Read the control back: does it show our value, and is there no validation error next to it?"""
        base: Root = root if root is not None else loc.page
        try:
            if f.type in {"radio", "checkbox_group"}:
                wanted = split_choices(value, f.option_labels) if f.type == "checkbox_group" else [value.strip()]
                for w in wanted:
                    opt = next((o for o in f.options if o.label.strip().lower() == w.lower()), None) or next(
                        (o for o in f.options if o.label == choose_option(w, f.option_labels)), None)
                    if opt is None:
                        return False, ""
                    by_group = await _radio_selected(base, f.name, opt.label) if f.type == "radio" and f.name else None
                    if by_group is None:
                        try:
                            by_group = await base.locator(f'[data-pja-id="{opt.id}"]').first.is_checked(timeout=1500)
                        except PlaywrightError:
                            by_group = False
                    if not by_group:
                        return False, ""
                return True, ""
            state = await loc.evaluate(_VERIFY_JS)
        except PlaywrightError as exc:
            return False, str(exc).splitlines()[0][:120]
        if f.type == "checkbox":
            want = value.strip().lower() in {"yes", "true", "y", "1", "i agree", "agree", "checked"}
            ok = (state["got"] == "__checked__") == want
        elif f.type in {"date", "month", "datetime-local", "time", "week", "combobox"}:
            ok = bool(state["got"])  # the widget reformats dates / shows its own label
        elif f.type == "number":
            ok = re.sub(r"[^\d.]", "", state["got"]) == re.sub(r"[^\d.]", "", value) or _same(state["got"], value)
        else:
            ok = _same(state["got"], value)
        problem = state.get("err") or ("marked invalid" if state.get("invalid") else "")
        return ok and not problem, problem

    async def _retry_set(self, loc: Locator, f: FormField, value: str, root: Root) -> bool:
        try:
            if f.type in {"text", "email", "tel", "url", "textarea", "number", "search"}:
                await loc.click()
                await loc.fill("")
                await loc.press_sequentially(value, delay=20)
                await loc.press("Tab")
                return True
            if f.type == "select":
                picked = choose_option(value, f.option_labels, min_score=70)
                opt = next((o for o in f.options if o.label == picked), None)
                if opt is not None:
                    await loc.select_option(value=opt.id, timeout=4000)
                    await _mark_chosen(loc)
                    return True
            if f.type in {"radio", "checkbox"}:
                return await self.set_value(loc, f, value, root=root)
        except PlaywrightError as exc:
            log.debug("retry failed for %s: %s", f.label, str(exc).splitlines()[0])
        return False

    async def _fill_file(self, loc: Locator, f: FormField, question: str) -> FilledField:
        label = f"{f.label} {f.name} {f.accept}"
        if COVER_RX.search(label):
            return FilledField(f, question or "Cover letter", None, "unresolved" if f.required else "skipped",
                               "no cover-letter file configured")
        if not f.is_empty:
            return FilledField(f, question or "Resume", None, "prefilled", f"file already attached: {f.value}")
        if images_only(f.accept) or f.id in self._not_documents or (
                PICTURE_RX.search(f"{f.label} {f.name}") and not RESUME_RX.search(label)):
            # a profile picture / avatar upload (SmartRecruiters puts one above the form): never given the résumé
            return FilledField(f, question or "Photo", None, "unresolved" if f.required else "skipped",
                               "a picture upload - no photo is configured (your résumé is not uploaded as a picture)")
        if self._resume_inputs and f.id not in self._resume_inputs:
            return FilledField(f, question or "Document", None, "unresolved" if f.required else "skipped",
                               "another upload on this form is the one for your résumé")
        if "resume" in self._handled and (self._file_attached or not (f.required or RESUME_RX.search(label))
                                          or OTHER_DOCUMENT_RX.search(label)):
            # (attached on an earlier step, or in another box of this one. A résumé box that is empty again - the
            # form was reset or redrawn after a refused submit - is filled again: intellohire answered "Please attach
            # your resume" because the first upload was remembered and the emptied box left alone)
            return FilledField(f, question or "Resume", None, "prefilled", "resume already provided")
        # the first document upload that is not a cover letter is the résumé upload
        if self.resume_path is None or not self.resume_path.is_file():
            return FilledField(f, question or "Resume", None, "unresolved", "no resume file available")
        before = set(await collect_errors(loc.page))
        await self._attach(loc, self.resume_path)
        await self.engine.human_delay(500, 1200)
        refused = next((e for e in await collect_errors(loc.page) if e not in before and _FORMAT_REFUSED.search(e)), "")
        if refused:
            # the site says it is not a document upload after all: take the file back and let the next one have it
            self._not_documents.add(f.id)
            try:
                await loc.set_input_files([])
            except PlaywrightError as exc:
                log.debug("could not clear a refused upload: %s", exc)
            return FilledField(f, question or "Upload", None, "unresolved" if f.required else "skipped",
                               f"this upload does not take a résumé (the site said: {refused[:120]})")
        self._handled.add("resume")
        return FilledField(f, question or "Resume", None, "uploaded", self.resume_path.name)

    async def _attach(self, loc: Locator, path: Path, *, again: bool = False) -> None:
        """Put a file into an upload box the way the page expects it.

        A file is normally put straight into the ``<input type=file>``, hidden or not. Some sites wire up their upload
        only when their own "Upload" button is clicked (intellohire: the click attaches the handler that sends the
        file and stores its token; a file put into the hidden input was ignored - "Please attach you resume"). So
        the button is used instead when it is explicitly bound to the hidden input (``data-input="#id"``), and when
        the site said the résumé is missing after the direct way (``again``): the button is clicked and the file
        given to the chooser it opens. When no chooser opens the file goes into the input itself."""
        page = loc.page
        try:
            kind = await loc.evaluate(_UPLOAD_TRIGGER_JS)
        except PlaywrightError:
            kind = ""
        if kind == "bound" or (again and kind):
            try:
                trigger = (await loc.evaluate_handle(
                    "el => el.getRootNode().querySelector('[data-pja-upload=\"1\"]')")).as_element()
                if trigger is None:
                    raise PlaywrightError("upload button is gone")
                async with page.expect_file_chooser(timeout=4000) as chooser:
                    await trigger.click(timeout=3000)
                await (await chooser.value).set_files(str(path))
                await self._upload_settled(loc)
                return
            except PlaywrightError as exc:
                log.debug("upload button opened no file chooser (%s): using the input itself", str(exc).splitlines()[0][:80])
        await loc.set_input_files(str(path))

    async def _upload_settled(self, loc: Locator, timeout_s: float = 10.0) -> None:
        """Wait (bounded) for the site's own upload of a chosen file: the token field the button names is filled,
        or the page's requests have gone quiet."""
        page = loc.page
        try:
            if await loc.evaluate(_UPLOAD_TOKEN_JS) is not None:
                deadline = asyncio.get_running_loop().time() + timeout_s
                while asyncio.get_running_loop().time() < deadline:
                    if await loc.evaluate(_UPLOAD_TOKEN_JS):
                        return
                    await page.wait_for_timeout(300)
                return
            await page.wait_for_load_state("networkidle", timeout=6000)
        except PlaywrightError:
            pass

    async def set_value(self, loc: Locator, f: FormField, value: str, *, root: Root | None = None) -> bool:
        """Set ``value`` on field ``f``. ``root`` (page, frame or scoped locator) locates sibling options."""
        base: Root = root if root is not None else loc.page
        try:
            if f.type in {"text", "email", "tel", "url", "number", "textarea", "password", "date", "month",
                          "datetime-local", "time", "week"}:
                if f.type == "number":
                    m = re.search(r"-?\d+(?:\.\d+)?", value.replace(",", ""))
                    if not m:
                        return False
                    value = m.group(0)
                before = await _suggestions(loc) if f.type in {"text", "search"} else None
                await loc.fill(value)
                current = await loc.input_value()
                if current.strip() != value.strip():
                    await loc.fill("")
                    await loc.press_sequentially(value, delay=15)
                if before is not None:
                    await self._pick_suggestion(loc, value, before)
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
                await _mark_chosen(loc)
                return True
            if f.type == "radio":
                opt = next((o for o in f.options if o.label.strip().lower() == value.strip().lower()), None)
                if opt is None:
                    picked = choose_option(value, f.option_labels)
                    opt = next((o for o in f.options if o.label == picked), None)
                if opt is None:
                    return False
                if await _check(base, opt.id):
                    return True
                # React re-rendered the group on selection (LinkedIn 2026): the input we clicked is gone - read the
                # group by its name instead
                return bool(f.name) and await _radio_selected(base, f.name, opt.label) is True
            if f.type == "checkbox":
                want = value.strip().lower() in {"yes", "true", "y", "1", "i agree", "agree", "checked"}
                is_checked = await loc.is_checked()
                if want != is_checked:
                    return await _check(base, f.id, uncheck=not want)
                return True
            if f.type == "checkbox_group":
                wanted = split_choices(value, f.option_labels)
                ok_any = False
                for w in wanted:
                    picked = choose_option(w, f.option_labels)
                    opt = next((o for o in f.options if o.label == picked), None)
                    if opt is not None:
                        ok_any = await _check(base, opt.id) or ok_any
                return ok_any
            if f.type == "combobox":
                parts = split_choices(value, f.option_labels) if f.multiple else []
                if len(parts) > 1:  # a tag box ("Looking for jobs in"): each value is typed and picked on its own
                    picked = [await self._fill_combobox(loc, part, widget=f.widget) for part in parts[:4]]
                    return any(picked)
                return await self._fill_combobox(loc, value, widget=f.widget)
        except PlaywrightError as exc:
            log.debug("set_value failed for %s: %s", f.label, str(exc).splitlines()[0])
            return False
        return False

    async def _pick_suggestion(self, loc: Locator, value: str, before: list[dict]) -> str | None:
        """Autocomplete boxes ('Start typing and select a location'): a typed value is not accepted until one of
        the suggestions that appear under the box is chosen. Pick the one that matches what we typed."""
        seen = {b["t"] for b in before}
        for _ in range(4):
            await loc.page.wait_for_timeout(300)
            fresh = [o for o in await _suggestions(loc) if o["t"] not in seen]
            if not fresh:
                continue
            texts = [o["t"] for o in fresh]
            low = (value or "").strip().lower()
            picked = next((t for t in texts if low and t.lower().startswith(low)), None) or \
                next((t for t in texts if low and low in t.lower()), None) or choose_option(value, texts, min_score=88)
            if picked is None:
                return None
            target = next(o for o in fresh if o["t"] == picked)
            try:
                await loc.page.locator(f'[data-pja-sugg="{target["i"]}"]').first.click(timeout=3000)
                await loc.page.wait_for_timeout(250)
                return picked
            except PlaywrightError as exc:
                log.debug("suggestion click failed: %s", exc)
                return None
        return None

    _OPTIONS = ("[role='option']:visible, [role='listbox'] li:visible, .select__option:visible, .pac-item:visible, "
                ".ui-menu-item:visible, [class*='autocomplete' i] li:visible, [class*='typeahead' i] li:visible")

    async def _is_text_input(self, loc: Locator) -> bool:
        try:
            return bool(await loc.evaluate("e => e.tagName === 'INPUT' || e.tagName === 'TEXTAREA' || e.isContentEditable"))
        except PlaywrightError:
            return False

    @staticmethod
    async def _in_modal(loc: Locator) -> bool:
        """Escape inside a modal dialog closes the dialog itself (LinkedIn: 'Save this application?')."""
        try:
            # (also dialogs that do not say so: Semantic UI ".ui.modal", Bootstrap ".modal", any fixed overlay box)
            return bool(await loc.evaluate(
                """e => { if (e.closest('dialog, [role=dialog], [role=alertdialog], [aria-modal=true], .modal, .ui.modal, ' +
                                         '[class*="modal" i], [class*="popup" i], [class*="drawer" i], [class*="lightbox" i]')) return true;
                          for (let n = e.parentElement; n && n !== document.body; n = n.parentElement)
                            if (getComputedStyle(n).position === 'fixed') return true;
                          return false; }"""))
        except PlaywrightError:
            return False

    async def _close_dropdown(self, loc: Locator) -> None:
        page = loc.page
        if await self._in_modal(loc):
            if await page.locator(self._OPTIONS).count():
                await loc.click()  # toggle it shut; never Escape inside a dialog
            return
        await page.keyboard.press("Escape")
        if await page.locator(self._OPTIONS).count():  # Escape did not close it: click the control again
            await loc.click()

    async def _peek_options(self, loc: Locator) -> list[str]:
        """Open a custom (non-typing) dropdown, read its options, close it again."""
        if await self._is_text_input(loc):
            return []  # typeahead: options only appear after typing
        page = loc.page
        try:
            await loc.click()
            await page.wait_for_timeout(400)
            options = page.locator(self._OPTIONS)
            texts = [((await options.nth(i).inner_text()) or "").strip() for i in range(min(await options.count(), 40))]
            await self._close_dropdown(loc)
            return [t for t in texts if t]
        except PlaywrightError:
            return []

    async def _fill_combobox(self, loc: Locator, value: str, *, widget: bool = False) -> bool:
        """Typeahead / react-select / button dropdowns: open (and type when it is a text box), pick the best option.

        The suggestions of a typeahead are fetched after typing and may take seconds (SmartRecruiters' "City" was
        given up on after 0.7 s and asked of the user although the profile had the answer): they are waited for.
        When a plain text box suggests nothing at all it keeps what was typed - a free-text box; a site that insists
        on a choice says so on Next / Submit, in its own words. The filter box of a dropdown widget (``widget``) is
        different: text left in it is not a value, so it is cleared and the field reported as not set."""
        page = loc.page
        typing = await self._is_text_input(loc)
        if typing:
            await _typed_suggestions(loc, value, baseline=True)  # what reads like the value before anything is typed
        try:
            await loc.click(timeout=800 if widget else 3000)
        except PlaywrightError:
            # something is drawn over the box (a dropdown's placeholder text covers its narrow search box): the
            # click cannot land, but the box can still be focused and typed into
            await loc.focus()
        if typing:
            try:
                await loc.fill("")
                await loc.press_sequentially(value[:40], delay=40)
            except PlaywrightError:
                await page.keyboard.type(value[:40], delay=40)
        options = page.locator(self._OPTIONS)
        count, texts, real = 0, [], []
        # (a dropdown widget draws plain entries without list roles: they are looked for further down, at once)
        for _ in range(0 if widget and typing else 10 if typing else 2):
            await page.wait_for_timeout(450)
            count = await options.count()
            texts = [((await options.nth(i).inner_text()) or "").strip() for i in range(min(count, 30))]
            real = [t for t in texts if t and not _NO_SUGGESTION.match(t)]
            if real:
                break
        picked = choose_option(value, real, min_score=70) if real else None
        if picked is not None:
            await options.nth(texts.index(picked)).click()
            return True
        if real and typing:
            await page.keyboard.press("Enter")  # suggestions that read differently: the first is what Enter takes
            return True
        if typing:
            # no list with the usual markup. Web-component design systems draw their suggestions as plain elements
            # (SmartRecruiters "City"), dropdown widgets as plain entries (Semantic UI): whatever appeared at the box
            # and reads like what was typed is the list.
            for attempt in range(9 if widget else 4):
                await page.wait_for_timeout(450 if widget else 0)
                shown = await _typed_suggestions(loc, value)
                # this box's own list; for a plain typeahead also what is drawn under it. Never anything else: another
                # box's shown value reappears when focus moves here, reads the same - and is not a suggestion
                shown = [c for c in shown if c.get("own")] or ([] if widget else [c for c in shown if c.get("below")])
                best = choose_option(value, [c["t"] for c in shown], min_score=70) if shown else None
                if best is not None:
                    target = next(c for c in shown if c["t"] == best)
                    try:
                        await page.locator(f'[data-pja-sugg="{target["i"]}"]').first.click(timeout=3000)
                        await page.wait_for_timeout(300)
                        return True
                    except PlaywrightError as exc:
                        log.debug("typed suggestion click failed: %s", exc)
                        break
                if widget and attempt >= 2 and await _widget_says_no_results(loc):
                    break  # the widget itself says there is nothing to choose
                if not widget:
                    await page.wait_for_timeout(600)
            if widget:
                # a dropdown's filter box, and nothing to choose: clear it (left as it is, the widget takes its first
                # entry on blur - intellohire's experience list became "Fresher") and report that nothing was set.
                # Never Escape here: inside a dialog it closes the dialog - and with it the whole application form.
                try:
                    await loc.fill("")
                except PlaywrightError:
                    pass
                await self._close_dropdown(loc)
                return False
            try:
                return bool((await loc.input_value()).strip())  # nothing suggested: the typed text stays
            except PlaywrightError:
                return False
        await self._close_dropdown(loc)
        return False


async def _widget_says_no_results(loc: Locator) -> bool:
    try:
        return bool(await loc.evaluate(
            r"""e => { const w = e.closest('.ui.dropdown, .select2-container, .chosen-container'); if (!w) return false;
                      const m = Array.from(w.querySelectorAll('.message, .no-results, .select2-results__message'))
                                     .find(x => x.getClientRects().length);
                      return !!m && /no\s+(results?|match|options?)|not\s+found/i.test(m.textContent || ''); }"""))
    except PlaywrightError:
        return False


_NO_SUGGESTION = re.compile(r"^(?:no\s+(?:results?|options?|match(?:es)?|data|suggestions?)\b|loading|searching|"
                            r"type\s+to\s+search|start\s+typing|please\s+enter)", re.I)
_EMAIL_VALUE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)


def _type_mismatch(f: FormField, value: str) -> str:
    """An answer that cannot belong in this control: the label was read from the wrong element."""
    v = (value or "").strip()
    if not v:
        return ""
    if f.type == "email" and not _EMAIL_VALUE.match(v):
        return f"this is an email box but the answer for '{f.label}' is not an email address - label probably misread"
    if f.type == "tel" and (_EMAIL_VALUE.match(v) or len(re.sub(r"\D", "", v)) < 6):
        return f"this is a phone box but the answer for '{f.label}' is not a phone number - label probably misread"
    return ""


_SECTION_COMPANY = re.compile(r"^(?:company|employer|company\s+name|organi[sz]ation|employer\s+name)\s*\*?$", re.I)
_SECTION_TITLE = re.compile(r"^(?:title|job\s+title|position|designation|role)\s*\*?$", re.I)


def _note_section(section: dict[str, str], label: str, value: str) -> None:
    """Remember the Company / Title of the entry being filled, for the fields that follow it."""
    if not (value or "").strip():
        return
    key = "company" if _SECTION_COMPANY.match(label or "") else "title" if _SECTION_TITLE.match(label or "") else ""
    if not key:
        return
    if key in section and len(section) == 2:
        section.clear()  # the next entry of the list begins
    section[key] = value.strip()


@dataclass
class InvalidReport:
    fixed: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)


_INVALID_JS = r"""
(container) => {
  const base = container || document;
  const out = [];
  base.querySelectorAll('input, select, textarea').forEach(el => {
    if (!el.getClientRects().length || ['hidden', 'submit', 'button'].includes(el.type)) return;
    const aria = el.getAttribute('aria-invalid') === 'true';
    const v = el.validity; const native = v && !v.valid && !v.valueMissing ? true : (v && v.valueMissing && el.required);
    let message = '';
    const ids = ((el.getAttribute('aria-errormessage') || '') + ' ' + (el.getAttribute('aria-describedby') || '')).split(/\s+/).filter(Boolean);
    for (const id of ids) { const n = document.getElementById(id); const t = n && n.getClientRects().length ? (n.innerText || '').trim() : ''; if (t) { message = t; break; } }
    if (!aria && !native && !(message && /required|invalid|enter\s+a|must|whole number|decimal|between|valid|select/i.test(message))) return;
    if (!message && native) message = el.validationMessage || '';
    out.push({ id: el.dataset.pjaId || '', label: (el.labels && el.labels[0] ? el.labels[0].innerText : el.getAttribute('aria-label') || '').trim().slice(0, 80), message: message.slice(0, 160) });
  });
  return out.slice(0, 12);
}
"""


_SUGGESTIONS_JS = r"""
(el) => {
  const r = el.getBoundingClientRect();
  const sels = '[role=option], [role=listbox] li, .pac-item, .ui-menu-item, .autocomplete-suggestion, ' +
               '[class*="suggestion" i] li, [class*="autocomplete" i] li, [class*="typeahead" i] li';
  const out = [];
  const stamp = 's' + Date.now().toString(36) + Math.floor(Math.random() * 1e6).toString(36) + '-';  // fresh per call
  document.querySelectorAll(sels).forEach((o, i) => {
    const b = o.getBoundingClientRect();
    if (!b.width || !b.height || o.contains(el)) return;
    if (b.top > r.bottom + 450 || b.bottom < r.top - 450) return;  // only lists next to this box
    const t = (o.innerText || '').replace(/\s+/g, ' ').trim();
    if (t && t.length < 160) { o.setAttribute('data-pja-sugg', stamp + i); out.push({ i: stamp + i, t }); }
  });
  return out;
}
"""


# The visible control that opens the file chooser of a hidden <input type=file>. Marks it data-pja-upload="1" and says
# how sure the link is: "bound" (the control names the input: data-input="#id") or "near" (an Upload/Attach/Browse
# control next to it). Other sources (Dropbox, Drive, LinkedIn, paste) and links that lead elsewhere are never it.
_UPLOAD_TRIGGER_JS = r"""
(el) => {
  const vis = (x) => !!x && x.getClientRects().length > 0 && getComputedStyle(x).visibility !== 'hidden';
  const root = el.getRootNode();
  root.querySelectorAll('[data-pja-upload]').forEach(x => x.removeAttribute('data-pja-upload'));
  if (vis(el)) return '';  // the box itself is on screen: files are simply put into it
  const text = (x) => (x.innerText || x.getAttribute('aria-label') || x.getAttribute('title') || '').slice(0, 80);
  const says = /upload|attach|browse|choose\s+file|select\s+file|add\s+file|resume|\bcv\b/i;
  const other = /dropbox|drive|linkedin|onedrive|box\.com|paste|manual|remove|delete|clear|cancel|view|download|sample|template/i;
  const leads = (x) => { const h = x.tagName === 'A' ? (x.getAttribute('href') || '') : ''; return !!h && !/^(#|javascript:)/i.test(h); };
  const usable = (x) => vis(x) && !x.contains(el) && !leads(x) && !other.test(text(x));
  let bound = [];
  if (el.id) { try { bound = [...root.querySelectorAll('[data-input="#' + CSS.escape(el.id) + '"], [data-file-input="#' +
                                                      CSS.escape(el.id) + '"]')].filter(usable); } catch (e) {} }
  if (bound.length) { bound[0].setAttribute('data-pja-upload', '1'); return 'bound'; }
  const near = [];
  if (el.id) { try { root.querySelectorAll('label[for="' + CSS.escape(el.id) + '"]').forEach(x => near.push(x)); } catch (e) {} }
  for (let wrap = el.parentElement, d = 0; wrap && d < 3 && near.length === 0; wrap = wrap.parentElement, d++) {
    if (wrap.querySelectorAll('input[type=file]').length > 1) break;  // a row of several uploads: not ours to guess
    wrap.querySelectorAll('button, [role=button], a, label, .button, [class*="upload" i], [class*="browse" i]').forEach(x => {
      if (says.test(text(x))) near.push(x); });
  }
  const trigger = near.find(usable);
  if (!trigger) return '';
  trigger.setAttribute('data-pja-upload', '1');
  return 'near';
}
"""

# the hidden field the marked upload control fills when the site has stored the file (data-target="#resume_hash"):
# null when it names none, otherwise that field's current value
_UPLOAD_TOKEN_JS = r"""
(el) => {
  const root = el.getRootNode();
  const trigger = root.querySelector('[data-pja-upload="1"]');
  const sel = trigger && (trigger.getAttribute('data-target') || trigger.getAttribute('data-hash'));
  if (!sel) return null;
  let target = null; try { target = root.querySelector(sel); } catch (e) {}
  return target && 'value' in target ? String(target.value || '') : null;
}
"""

_TYPED_SUGGESTIONS_JS = r"""
(el, args) => {
  // baseline = true (before typing): remember what already reads like the value - page text, the choice shown in
  // another box - so that only what appears afterwards counts as this box's suggestions
  const [typed, baseline] = args;
  const box = el.getBoundingClientRect(); const low = (typed || '').trim().toLowerCase(); const out = [];
  if (!low) return out;
  const exact = low.length < 3;  // "7" in a list of years: the entry that says exactly that, not "17" or "27"
  const T = (n) => (n.innerText || n.textContent || '').replace(/\s+/g, ' ').trim();
  const visit = (root, depth) => { root.querySelectorAll('*').forEach(n => {
    if (n.shadowRoot && depth < 6) visit(n.shadowRoot, depth + 1);
    if (n === el || /^(SCRIPT|STYLE|INPUT|TEXTAREA|SELECT|OPTION|LABEL|HTML|BODY|FORM)$/.test(n.tagName)) return;
    const t = T(n);
    if (!t || t.length > 120 || !(exact ? t.toLowerCase() === low : t.toLowerCase().includes(low))) return;
    const b = n.getBoundingClientRect();
    if (!b.width || !b.height) return;
    // (everything that reads like the value before typing is remembered, wherever it is: the box may move or grow)
    if (baseline) { n.setAttribute('data-pja-old', stamp); return; }
    if (n.getAttribute('data-pja-old')) return;  // it was there before anything was typed
    // a list drawn under (or just over) this box, overlapping it sideways. Judged by the entry's whole row (the
    // outermost element that says exactly the same): the text itself may be a short span at the row's left edge
    let row = n;
    while (row.parentElement && T(row.parentElement) === t) row = row.parentElement;
    const rb = row.getBoundingClientRect();
    if (rb.top > box.bottom + 500 || rb.bottom < box.top - 320 || rb.right < box.left - 40 || rb.left > box.right + 40) return;
    // only the innermost elements that carry the text: a list, a menu or a whole field that merely contains a
    // matching entry is not an entry (clicking a menu's box selects nothing)
    if ([...n.querySelectorAll('*')].some(c => { const ct = T(c).toLowerCase(); return exact ? ct === low : ct.includes(low); })) return;
    // (a fresh mark per call: the list of an earlier box - now hidden - must never be the one that gets clicked)
    const i = stamp + out.length; n.setAttribute('data-pja-sugg', i);
    // where it is: inside this box's own widget (or its component), and under the box rather than over it
    const own = !!widget && (widget.contains(n) || n.getRootNode() === el.getRootNode() && !!el.getRootNode().host);
    out.push({ i, t, own, below: b.top >= box.bottom - 6 });
  }); };
  const widget = el.closest('.ui.dropdown, .select2-container, .chosen-container, [role="combobox"], [class*="autocomplete" i], ' +
                            '[class*="typeahead" i], [class*="dropdown" i], [class*="combobox" i]') || el.getRootNode().host || null;
  const stamp = 't' + Date.now().toString(36) + Math.floor(Math.random() * 1e6).toString(36) + '-';
  if (baseline) { const clear = (root, depth) => root.querySelectorAll('*').forEach(n => { if (n.shadowRoot && depth < 6) clear(n.shadowRoot, depth + 1);
    if (n.hasAttribute('data-pja-old')) n.removeAttribute('data-pja-old'); }); clear(document, 0); }
  visit(document, 0);
  return out.slice(0, 20);
}
"""


async def _typed_suggestions(loc: Locator, typed: str, *, baseline: bool = False) -> list[dict]:
    """Visible elements next to a text box whose text contains what was typed (through open shadow roots) and that
    were not there at the ``baseline`` call made before typing."""
    try:
        return await loc.evaluate(_TYPED_SUGGESTIONS_JS, [typed, baseline])
    except PlaywrightError:
        return []


async def _suggestions(loc: Locator) -> list[dict]:
    try:
        return await loc.evaluate(_SUGGESTIONS_JS)
    except PlaywrightError:
        return []


def _root_locator(root: Root, selector: str) -> Locator:
    return root.locator(selector).first


async def _check(root: Root, pja_id: str, uncheck: bool = False) -> bool:
    loc = root.locator(f'[data-pja-id="{pja_id}"]').first
    try:
        if uncheck:
            await loc.uncheck(timeout=3000)
        else:
            await loc.check(timeout=3000)
        return True
    except PlaywrightError:
        pass
    try:
        if not await loc.count():  # replaced by a re-render after the click: the caller reads the group instead
            return False
    except PlaywrightError:
        return False
    # custom-styled inputs are often visually hidden: click the label instead
    want = not uncheck

    async def state() -> bool:
        try:
            return bool(await loc.evaluate("el => el.checked", timeout=1000)) == want
        except PlaywrightError:  # (also: the input was replaced by a re-render)
            return False

    try:
        await loc.evaluate("""(el, uncheck) => {
                 if (el.checked === !uncheck) return;
                 const lbl = (el.id && el.getRootNode().querySelector('label[for="' + CSS.escape(el.id) + '"]')) || el.closest('label');
                 (lbl || el).click();
               }""", uncheck)
    except PlaywrightError:
        pass
    for _ in range(4):  # React-controlled inputs re-render after the event
        if await state():
            return True
        await loc.page.wait_for_timeout(150)
    try:  # a real pointer click on the visible label (or the box itself)
        lbl = loc.page.locator(f'label[for="{await loc.get_attribute("id")}"]').first if await loc.get_attribute("id") else None
        target = lbl if lbl is not None and await lbl.count() else loc
        await target.click(force=True, timeout=3000)
    except PlaywrightError:
        return await state()
    for _ in range(4):
        if await state():
            return True
        await loc.page.wait_for_timeout(150)
    return False


_RADIO_GROUP_JS = r"""
(container, [name, label]) => {
  const scope = container || document;
  const T = (e) => e ? (e.innerText || e.textContent || '').replace(/\s+/g, ' ').trim() : '';
  const norm = (t) => (t || '').trim().toLowerCase();
  const radios = Array.from(scope.querySelectorAll('input[type="radio"]')).filter(r => r.name === name);
  if (!radios.length) return null;
  for (const r of radios) {   // the option's text, read the way the scan reads it
    let text = T((r.id && r.getRootNode().querySelector('label[for="' + CSS.escape(r.id) + '"]')) || r.closest('label'));
    let box = r.parentElement;
    for (let d = 0; d < 4 && box && !text; d++, box = box.parentElement) {
      if (box.querySelectorAll('input[type="radio"]').length !== 1) break;
      const rr = box.querySelector('[role="radio"][aria-label]');
      text = (rr && rr.getAttribute('aria-label')) || (T(box).length <= 120 ? T(box) : '');
    }
    if (norm(text || r.value) === norm(label)) {
      const wrap = r.closest('[role="radio"]');
      return !!r.checked || (!!wrap && wrap.getAttribute('aria-checked') === 'true');
    }
  }
  return null;
}
"""


async def _radio_selected(root: Root, name: str, label: str) -> bool | None:
    """Is the option ``label`` of radio group ``name`` selected? None when the group / option cannot be found."""
    try:
        if isinstance(root, Locator):
            return await root.evaluate(_RADIO_GROUP_JS, [name, label], timeout=2000)
        return await root.evaluate(f"(args) => ({_RADIO_GROUP_JS})(null, args)", [name, label])
    except PlaywrightError:
        return None


def summarize_unresolved(report: FillReport) -> list[str]:
    return [f"{i.question or i.field.name}: {i.note}" for i in report.unresolved]


__all__ = ["FormField", "FormFiller", "FillReport", "FilledField", "scan_fields", "collect_errors",
           "summarize_unresolved", "real_options"]
