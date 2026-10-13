"""Page observation: one structured snapshot of whatever the browser currently shows.

The snapshot covers the main document, open shadow roots and relevant iframes (embedded
application forms), and lists every visible clickable element and form field with stable ids
so the navigator (or the local LLM) can refer to them unambiguously.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass, field

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Frame, Page

from app.browser.forms import FormField, scan_fields
from app.navigator.classify import (
    COOKIE_CONTEXT_RX,
    Kind,
    already_applied_evidence,
    classify_element,
    expired_evidence,
    is_application_field,
    success_evidence,
)

log = logging.getLogger(__name__)

_OBSERVE_JS = r"""
(args) => {
  const T = (el) => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  const shown = (el) => {
    if (!el || !el.isConnected || el.nodeType !== 1) return false;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity || '1') === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 1 && r.height > 1;
  };
  const roots = [];
  const addRoot = (r) => { roots.push(r); r.querySelectorAll('*').forEach(e => { if (e.shadowRoot) addRoot(e.shadowRoot); }); };
  addRoot(document);
  const DIALOG = '[role="dialog"], [role="alertdialog"], dialog[open], [aria-modal="true"], .modal.show, .modal.in, .modal.open, .modal[style*="display: block"]';
  const SEL = 'a[href], button, [role="button"], [role="link"], [role="menuitem"], input[type="submit"], input[type="button"], input[type="image"], summary, [onclick], [tabindex="0"][class*="btn" i], [class*="button" i][tabindex]';
  const out = []; const seen = new Set();
  const formKey = (form) => { if (!form) return ''; if (!form.dataset.pjaForm) { window.__pjaFormSeq = (window.__pjaFormSeq || 0) + 1; form.dataset.pjaForm = 'f' + window.__pjaFormSeq; } return form.dataset.pjaForm; };
  const ctxOf = (el) => { const c = el.closest('header, nav, footer, [role="navigation"], [role="banner"], [role="contentinfo"]'); return c ? (c.getAttribute('role') || c.tagName.toLowerCase()) : ''; };
  // heading of the card / section a button belongs to (career pages list many jobs, each with "Apply")
  const sectionOf = (el) => { let n = el.parentElement;
    for (let d = 0; d < 7 && n && n !== document.body; d++, n = n.parentElement) {
      const hs = Array.from(n.querySelectorAll('h1, h2, h3, h4, h5, [role="heading"]')).filter(h => !h.contains(el) && (h.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING));
      if (hs.length) return T(hs[hs.length - 1]).slice(0, 140);
    } return ''; };
  for (const r of roots) {
    for (const el of r.querySelectorAll(SEL)) {
      if (seen.has(el)) continue; seen.add(el);
      if (!shown(el)) continue;
      // a clickable nested in another clickable: keep the outer one only
      const parentClickable = el.parentElement && el.parentElement.closest('a[href], button, [role="button"]');
      if (parentClickable && seen.has(parentClickable) && parentClickable !== el) continue;
      let text = el.tagName === 'INPUT' ? (el.value || '') : T(el);
      const aria = el.getAttribute('aria-label') || el.getAttribute('title') || '';
      if (!text) { const img = el.querySelector && el.querySelector('img[alt], svg[aria-label]'); if (img) text = img.getAttribute('alt') || img.getAttribute('aria-label') || ''; }
      if (!el.dataset.pjaAct) { window.__pjaActSeq = (window.__pjaActSeq || 0) + 1; el.dataset.pjaAct = 'a' + window.__pjaActSeq; }
      const rect = el.getBoundingClientRect();
      const form = el.closest('form');
      const dlg = el.closest(DIALOG);
      out.push({
        id: el.dataset.pjaAct, tag: el.tagName.toLowerCase(), role: el.getAttribute('role') || '',
        text: text.slice(0, 140), aria: aria.slice(0, 120), href: (el.getAttribute('href') || '').slice(0, 300),
        // effective type: a <button> without type= inside a form *is* a submit button
        type: ((el.tagName === 'BUTTON' || el.tagName === 'INPUT') ? (el.type || '') : (el.getAttribute('type') || '')).toLowerCase(),
        target: el.getAttribute('target') || '',
        form: formKey(form), dialog: !!dlg, disabled: !!(el.disabled || el.getAttribute('aria-disabled') === 'true'),
        y: Math.round(rect.top + window.scrollY), x: Math.round(rect.left), w: Math.round(rect.width), h: Math.round(rect.height),
        ctx: ctxOf(el), section: sectionOf(el),
        hint: [el.id || '', el.getAttribute('name') || '', el.getAttribute('data-automation-id') || '', el.getAttribute('data-testid') || '', el.getAttribute('data-qa') || '', (typeof el.className === 'string' ? el.className : '')].join(' ').slice(0, 160),
      });
      if (out.length >= args.maxElements) break;
    }
    if (out.length >= args.maxElements) break;
  }
  // JS-only controls: <span>/<div> with a click handler and a pointer cursor ("Apply" on many SPA job boards)
  const ACTION_RX = /^(?:(?:quick |easy |1-click )?apply(?: now| here| online)?|apply for (?:this |the )?(?:job|position|role|opening)|i'?m interested|continue|next|submit(?: application)?|start (?:your )?application|apply on (?:the )?company (?:site|website)|apply as (?:a )?guest|apply without (?:an )?account)\s*[!»›>→]*$/i;
  for (const r of roots) {
    for (const el of r.querySelectorAll('span, div, p, li, i, label, strong, b, em, a:not([href])')) {
      if (out.length >= args.maxElements) break;
      if (seen.has(el) || el.closest('a[href], button, [role="button"]')) continue;
      const t = T(el);
      if (!t || t.length > 40 || !ACTION_RX.test(t)) continue;
      if (Array.from(el.children).some(c => T(c) === t)) continue;  // keep the innermost element
      if (!shown(el) || getComputedStyle(el).cursor !== 'pointer') continue;
      seen.add(el);
      if (!el.dataset.pjaAct) { window.__pjaActSeq = (window.__pjaActSeq || 0) + 1; el.dataset.pjaAct = 'a' + window.__pjaActSeq; }
      const rect = el.getBoundingClientRect();
      out.push({ id: el.dataset.pjaAct, tag: el.tagName.toLowerCase(), role: 'button', text: t.slice(0, 140), aria: '', href: '',
                 type: '', target: '', form: formKey(el.closest('form')), dialog: !!el.closest(DIALOG), disabled: false,
                 y: Math.round(rect.top + window.scrollY), x: Math.round(rect.left), w: Math.round(rect.width), h: Math.round(rect.height),
                 ctx: ctxOf(el), section: sectionOf(el), hint: 'js-click ' + (typeof el.className === 'string' ? el.className : '').slice(0, 120) });
    }
  }
  const fields = [];
  for (const r of roots) {
    r.querySelectorAll('[data-pja-id]').forEach(el => {
      const form = el.closest('form');
      const rect = el.getBoundingClientRect();
      // custom upload widgets hide the raw <input type=file> but show its label / wrapper; styled checkboxes and
      // radios hide the real input the same way - inside a web component, what is shown is the component itself
      const host = el.getRootNode().host || null;
      const vis = shown(el) || (el.type === 'file' && (shown(el.closest('label')) || shown(el.parentElement))) ||
                  (['checkbox', 'radio', 'file'].includes(el.type) && (shown(el.closest('label')) || shown(el.parentElement) || shown(host)));
      fields.push({ id: el.dataset.pjaId, form: formKey(form), dialog: !!el.closest(DIALOG), ctx: ctxOf(el),
                    y: Math.round(rect.top + window.scrollY), visible: vis });
    });
  }
  const dialogs = Array.from(document.querySelectorAll(DIALOG)).filter(shown).map(d => T(d).slice(0, 600));
  const pw = []; for (const r of roots) r.querySelectorAll('input[type="password"]').forEach(e => { if (shown(e)) pw.push((e.getAttribute('name') || '') + ' ' + (e.getAttribute('id') || '') + ' ' + (e.getAttribute('placeholder') || '') + ' ' + (e.getAttribute('autocomplete') || '')); });
  const headings = Array.from(document.querySelectorAll('h1, h2, h3, [role="heading"], legend')).filter(shown).map(T).filter(Boolean).map(t => t.slice(0, 160)).slice(0, 15);
  const body = document.body ? (document.body.innerText || '') : '';
  return { url: location.href, title: document.title || '', text: body.slice(0, args.textLimit), textLength: body.length,
           elements: out, fieldMeta: fields, dialogs, passwords: pw, headings,
           nodes: document.getElementsByTagName('*').length };
}
"""

_SKIP_FRAME_RX = re.compile(
    r"about:blank|doubleclick|googletagmanager|google-analytics|googlesyndication|facebook\.com/tr|youtube|vimeo|"
    r"hotjar|clarity\.ms|recaptcha|hcaptcha|challenges\.cloudflare|arkoselabs|intercom|drift\.com|zendesk|"
    r"livechat|tawk\.to|hubspot|cookielaw|onetrust|cookiebot|trustarc|usercentrics|linkedin\.com/px|bing\.com|"
    r"twitter|adsrvr|criteo|taboola|outbrain|newrelic|nr-data|sentry", re.I)


@dataclass
class Element:
    id: str
    frame: int
    tag: str
    text: str
    aria: str = ""
    role: str = ""
    href: str = ""
    type: str = ""
    target: str = ""
    form: str = ""
    dialog: bool = False
    disabled: bool = False
    y: int = 0
    x: int = 0
    w: int = 0
    h: int = 0
    ctx: str = ""
    hint: str = ""
    section: str = ""
    kind: Kind = Kind.OTHER

    @property
    def label(self) -> str:
        return (self.text or self.aria or self.hint or self.tag).strip()[:90]

    @property
    def selector(self) -> str:
        return f'[data-pja-act="{self.id}"]'

    def describe(self) -> str:
        bits = [f"[{self.id}]", self.tag + (f"[{self.type}]" if self.type else ""), repr(self.label)]
        if self.dialog:
            bits.append("(in dialog)")
        if self.form:
            bits.append("(in form)")
        if self.ctx:
            bits.append(f"(in {self.ctx})")
        if self.href and not self.href.startswith("#") and not self.href.startswith("javascript"):
            bits.append(f"-> {self.href[:80]}")
        return " ".join(bits)


@dataclass
class FieldInfo:
    field: FormField
    frame: int
    form: str = ""
    dialog: bool = False
    ctx: str = ""
    y: int = 0
    application: bool = True  # False for search boxes, newsletter inputs, header/footer widgets


@dataclass
class FrameInfo:
    index: int
    frame: Frame
    url: str


@dataclass
class Snapshot:
    url: str
    title: str
    text: str
    headings: list[str]
    elements: list[Element]
    fields: list[FieldInfo]
    dialogs: list[str]
    password_fields: list[str]
    frames: list[FrameInfo]
    signature: str
    text_length: int = 0
    success: str = ""
    already_applied: str = ""
    expired: str = ""
    cookie_banner: bool = False
    other_forms: list[str] = field(default_factory=list)  # "job search" / "contact" forms seen (never filled)

    # ------------------------------------------------------------ queries
    def frame(self, index: int) -> Frame:
        return self.frames[index].frame

    @property
    def app_fields(self) -> list[FieldInfo]:
        return [f for f in self.fields if f.application]

    @property
    def dialog_open(self) -> bool:
        return bool(self.dialogs)

    def by_kind(self, *kinds: Kind, enabled_only: bool = True) -> list[Element]:
        return [e for e in self.elements if e.kind in kinds and (not enabled_only or not e.disabled)]

    def element(self, element_id: str) -> Element | None:
        return next((e for e in self.elements if e.id == element_id), None)

    @property
    def password_count(self) -> int:
        return len(self.password_fields)

    def summary(self) -> str:
        return (f"{self.url} | fields={len(self.app_fields)} buttons={len(self.elements)} dialogs={len(self.dialogs)} "
                f"passwords={self.password_count}")


async def _evaluate(frame: Frame, js: str, arg, timeout: float = 6.0):
    return await asyncio.wait_for(frame.evaluate(js, arg), timeout)


async def _frame_is_relevant(index: int, frame: Frame) -> bool:
    if index == 0:
        return True
    url = frame.url or ""
    if not url or _SKIP_FRAME_RX.search(url) or frame.is_detached():
        return False
    try:
        el = await asyncio.wait_for(frame.frame_element(), 3)
        box = await el.bounding_box()
        return bool(box and box["width"] >= 120 and box["height"] >= 80)
    except (PlaywrightError, asyncio.TimeoutError):
        return False


async def observe(page: Page, *, text_limit: int = 6000, max_elements: int = 220, max_frames: int = 6) -> Snapshot:
    """Take a structured snapshot of ``page`` (main document + relevant iframes)."""
    frames: list[FrameInfo] = []
    elements: list[Element] = []
    fields: list[FieldInfo] = []
    dialogs: list[str] = []
    passwords: list[str] = []
    headings: list[str] = []
    texts: list[str] = []
    text_length = 0
    nodes = 0
    url, title = page.url, ""
    all_frames = [page.main_frame] + [f for f in page.frames if f is not page.main_frame]
    for frame in all_frames:
        index = len(frames)
        if index >= max_frames or not await _frame_is_relevant(index, frame):
            continue
        # fields first: scan_fields tags them with data-pja-id, the observer then reads their placement
        try:
            raw_fields = await asyncio.wait_for(scan_fields(frame), 8)
        except (PlaywrightError, asyncio.TimeoutError):
            raw_fields = []
        try:
            data = await _evaluate(frame, _OBSERVE_JS, {"textLimit": text_limit, "maxElements": max_elements})
        except (PlaywrightError, asyncio.TimeoutError) as exc:
            if index == 0:
                log.debug("observe main frame failed: %s", str(exc).splitlines()[0] if str(exc) else exc)
                data = {"url": page.url, "title": "", "text": "", "elements": [], "fieldMeta": [], "dialogs": [],
                        "passwords": [], "headings": [], "textLength": 0, "nodes": 0}
            else:
                continue
        if index > 0 and not raw_fields and not data.get("elements"):
            continue
        frames.append(FrameInfo(index=index, frame=frame, url=frame.url))
        if index == 0:
            url, title = data.get("url") or page.url, data.get("title") or ""
        texts.append(data.get("text") or "")
        text_length += int(data.get("textLength") or 0)
        nodes += int(data.get("nodes") or 0)
        dialogs.extend(data.get("dialogs") or [])
        passwords.extend(data.get("passwords") or [])
        headings.extend(data.get("headings") or [])
        meta = {m["id"]: m for m in data.get("fieldMeta") or []}
        for f in raw_fields:
            m = meta.get(f.id, {})
            info = FieldInfo(field=f, frame=index, form=m.get("form", ""), dialog=bool(m.get("dialog")),
                             ctx=m.get("ctx", ""), y=int(m.get("y") or 0))
            info.application = (is_application_field(f.label, f.name, f.placeholder)
                                and info.ctx not in {"header", "nav", "navigation", "banner"}
                                and f.type not in {"password", "search"}
                                and m.get("visible", True) is not False)
            if info.ctx in {"footer", "contentinfo"} and not info.dialog:
                info.application = False
            fields.append(info)
        for d in data.get("elements") or []:
            elements.append(Element(id=d["id"], frame=index, tag=d.get("tag", ""), text=d.get("text", ""),
                                    aria=d.get("aria", ""), role=d.get("role", ""), href=d.get("href", ""),
                                    type=d.get("type", ""), target=d.get("target", ""), form=d.get("form", ""),
                                    dialog=bool(d.get("dialog")), disabled=bool(d.get("disabled")),
                                    y=int(d.get("y") or 0), x=int(d.get("x") or 0), w=int(d.get("w") or 0),
                                    h=int(d.get("h") or 0), ctx=d.get("ctx", ""), hint=d.get("hint", ""),
                                    section=d.get("section", "")))
    full_text = "\n".join(t for t in texts if t)
    cookie_banner = any(COOKIE_CONTEXT_RX.search(d) for d in dialogs) or bool(
        COOKIE_CONTEXT_RX.search(full_text[-1500:] + full_text[:1500]))
    snap = Snapshot(url=url, title=title, text=full_text, headings=list(dict.fromkeys(headings))[:20],
                    elements=elements, fields=fields, dialogs=dialogs, password_fields=passwords, frames=frames,
                    signature="", text_length=text_length, cookie_banner=cookie_banner)
    _mark_non_application_forms(snap)
    _classify_elements(snap)
    snap.success = success_evidence(full_text)
    snap.already_applied = already_applied_evidence(full_text)
    snap.expired = expired_evidence(full_text[:4000])
    sig_src = "|".join([url, title, str(text_length // 20), str(nodes // 10), str(len(snap.app_fields)),
                        str(len(dialogs)), ",".join(sorted(f.field.label[:30] for f in snap.app_fields))[:400],
                        ",".join(headings[:5])])
    snap.signature = hashlib.sha1(sig_src.encode(), usedforsecurity=False).hexdigest()  # a page fingerprint
    return snap


_STRONG_SEARCH_FIELD = re.compile(
    r"^\s*(?:keywords?|search(?:\s+(?:jobs?|by|for|keywords?))?|sort(?:\s+(?:order|by))?|within|radius|distance|"
    r"date\s+posted|posted\s+(?:within|date)|work\s+model|industry|department|job\s+(?:category|function|family)|"
    r"category|business\s+unit)\s*\*?\s*$", re.I)
_ANY_OPTION = re.compile(r"^\s*(?:all|any)(?:\s+[\w ]{1,30})?\s*$", re.I)
_CONTACT_FIELD = re.compile(r"company\s+size|team\s+size|budget|how\s+can\s+we\s+help|project\s+(?:details|description|brief)|"
                            r"^subject\b|services?\s+(?:interested|required|needed)|enquiry|inquiry|your\s+(?:company|organi[sz]ation)\b|"
                            r"what\s+stage|which\s+stage|monthly\s+traffic|annual\s+revenue", re.I)
# fields only a sales / enquiry form asks for: with them (and no document upload) a form is never a job application,
# whatever the page around it says (dharmainfoservices.com, run #38: "Budget" on a careers page was asked of the user)
_SALES_FIELD = re.compile(r"\bbudget\b|company\s+size|team\s+size|how\s+can\s+we\s+help|project\s+(?:details|description|brief|type)|"
                          r"services?\s+(?:interested|required|needed)|monthly\s+traffic|annual\s+revenue|what\s+stage|which\s+stage", re.I)
_APPLY_HEADING = re.compile(r"\bapply\b|application|resume|\bcv\b|\bposition\b|vacanc|opening|\bjob\b", re.I)


def _mark_non_application_forms(snap: Snapshot) -> None:
    """Job-search filters and contact / sales forms sit on many career pages next to (or instead of) the real
    application. Their fields are never filled and their buttons never submitted."""
    from app.navigator.classify import CONTACT_BUTTON_RX, SEARCH_BUTTON_RX, norm

    groups: dict[tuple[int, str], list[FieldInfo]] = {}
    for f in snap.fields:
        if f.application:
            groups.setdefault((f.frame, f.form), []).append(f)
    # the strongest application-looking form on the page (a résumé upload, or the most fields)
    def app_score(fs: list[FieldInfo]) -> int:
        return 10 * any(x.field.type == "file" for x in fs) + len(fs)
    best_key = max(groups, key=lambda k: app_score(groups[k])) if len(groups) > 1 else None
    for (frame, form), fields in groups.items():
        buttons = [norm(e.text or e.aria) for e in snap.elements if e.frame == frame and (e.form == form if form else
                   not e.form and _near_fields(e, snap))]
        has_file = any(f.field.type == "file" for f in fields)
        search_fields = [f for f in fields if _STRONG_SEARCH_FIELD.match(f.field.label or "")
                         or (f.field.type == "select" and any(_ANY_OPTION.match(o) for o in f.field.option_labels))]
        search_button = any(SEARCH_BUTTON_RX.search(b) for b in buttons if b)
        contact_button = any(CONTACT_BUTTON_RX.search(b) for b in buttons if b)
        contact_fields = any(_CONTACT_FIELD.search(f.field.label or "") for f in fields) and not any(
            re.search(r"resume|\bcv\b|cover\s+letter|notice|experience|ctc|salary", f.field.label or "", re.I)
            for f in fields)
        about_applying = bool(_APPLY_HEADING.search(" ".join(snap.headings[:12])))
        # "How can we help?" next to a real application form on the same page is the site's enquiry form
        beside_application = best_key is not None and best_key != (frame, form)
        sales_fields = any(_SALES_FIELD.search(f.field.label or "") for f in fields)
        contact = contact_button or sales_fields or (contact_fields and (not about_applying or beside_application))
        if form:
            if not has_file and (search_button or (search_fields and len(search_fields) * 2 >= len(fields))):
                purpose = "job search"
            elif not has_file and contact:
                purpose = "contact"
            else:
                continue
            for f in fields:
                f.application = False
            snap.other_forms.append(purpose)
            continue
        # formless pages: drop the filter controls; the rest stays unless it is only a contact form
        for f in search_fields:
            f.application = False
        if search_fields:
            snap.other_forms.append("job search")
        rest = [f for f in fields if f.application]
        if rest and not has_file and contact:
            for f in rest:
                f.application = False
            snap.other_forms.append("contact")


def _classify_elements(snap: Snapshot) -> None:
    form_ids = {f.form for f in snap.app_fields if f.form}
    dialog_with_fields = any(f.dialog for f in snap.app_fields)
    has_app_fields = bool(snap.app_fields)
    for e in snap.elements:
        in_form_context = bool(
            (e.form and e.form in form_ids)
            or (e.dialog and dialog_with_fields)
            or (has_app_fields and not e.form and e.tag in {"button", "input"} and not e.ctx and _near_fields(e, snap)))
        cookie_ctx = snap.cookie_banner and (e.dialog or e.ctx in {"", "footer", "contentinfo"})
        e.kind = classify_element(e.text, tag=e.tag, type_=e.type if e.form else "", href=e.href, aria=e.aria,
                                  form_context=in_form_context, cookie_context=cookie_ctx)
        if e.kind == Kind.COOKIES and not snap.cookie_banner:
            e.kind = Kind.OTHER


def _near_fields(e: Element, snap: Snapshot) -> bool:
    """Formless SPAs: a button just below the visible application fields belongs to that form."""
    ys = [f.y for f in snap.app_fields if f.frame == e.frame and f.field.type != "file"]
    if not ys:
        return False
    return min(ys) - 50 <= e.y <= max(ys) + 900
