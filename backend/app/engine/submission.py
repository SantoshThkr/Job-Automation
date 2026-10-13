"""Did the application really go through? One verification contract for every submit in the system.

    SUBMIT_CLICKED -> WAITING_FOR_SUBMISSION_RESULT -> CONFIRMED | UNCERTAIN | FAILED
                                                   (+ REFUSED: validation messages, nothing was sent - fix & resubmit
                                                      NEXT_STEP: the submit opened another step of the form
                                                      NO_EFFECT: the click did nothing at all - may click once more)

A job becomes APPLIED only on CONFIRMED, i.e. genuine evidence:

strong      - the portal's / ATS's own confirmation (adapter probe), a *new* application-specific confirmation message
              in a confirmation region (dialog, alert/status, toast, heading, success box - never inside a job
              description), an application / reference number, the submit request's response saying it succeeded
              with an application id
supporting  - a confirmation-style URL (/thank-you, /success, /confirmation...), a new generic "Thank you!" heading,
              the form disappearing, the submit button disappearing, the submit request answered 2xx / success flag

Two supporting signals of different kinds (one of them visible content or the request result) also confirm. A URL
change, a vanished form or a navigation on its own never does. An explicit failure message ("Failed to submit
application", "There was an error trying to send...") is FAILED. Everything else after the wait is UNCERTAIN:
"Submitted - confirmation not verified", never resubmitted, never called FAILED.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Frame, Page, Request, Response

log = logging.getLogger(__name__)


class SubmissionState(StrEnum):
    SUBMIT_CLICKED = "SUBMIT_CLICKED"
    WAITING_FOR_SUBMISSION_RESULT = "WAITING_FOR_SUBMISSION_RESULT"
    CONFIRMED = "CONFIRMED"
    UNCERTAIN = "UNCERTAIN"
    FAILED = "FAILED"
    REFUSED = "REFUSED"
    NEXT_STEP = "NEXT_STEP"
    NO_EFFECT = "NO_EFFECT"
    BLOCKED = "BLOCKED"  # a CAPTCHA challenge appeared after the click: only you can continue


# application-specific confirmation wording (strong only when new and in a confirmation region)
APPLICATION_SUCCESS = (
    r"(?:your\s+)?application\s+(?:for\s+(?:the\s+)?[\w.#+/&,()' -]{2,80}?\s+)?(?:has\s+been\s+|was\s+|is\s+)?"
    r"(?:successfully\s+)?(?:sent|submitted|received|complete[d]?)|"
    r"successfully\s+(?:applied|submitted)|applied\s+successfully|thank(?:s|\s+you)\s+for\s+(?:applying|your\s+application|"
    r"your\s+interest\s+in\s+(?:this|the)\s+(?:role|position|job))|we(?:'ve|\s+have)\s+(?:successfully\s+)?received\s+your\s+"
    r"(?:application|resume|cv|profile)|you(?:'ve|\s+have)\s+(?:successfully\s+)?applied|"
    r"(?:the\s+|your\s+)?(?:form|details)\s+(?:was\s+|were\s+|has\s+been\s+|have\s+been\s+)?(?:sent|submitted)\s+successfully|"
    r"(?:resume|cv|profile)\s+(?:has\s+been|was)\s+(?:successfully\s+)?(?:sent|submitted|received)")
# generic acknowledgement: supporting only
GENERIC_THANKS = r"^\s*(?:thank\s*you|thanks)\b[!.,\s]*(?:[\u2014\u2013-]+\s*)?(?:we(?:'ve|\s+have)\s+(?:got|received)\s+it)?" \
                 r"[!.\s]*$|we(?:'ve|\s+have)\s+got\s+(?:it|your\s+(?:application|details|resume|cv|submission))\b|" \
                 r"we(?:'|\s+wi)ll\s+(?:get\s+back|be\s+in\s+touch|contact\s+you|review)|(?:our|a|the)\s+(?:member\s+of\s+" \
                 r"(?:our|the)\s+|relevant\s+|dedicated\s+)?(?:team(?:\s+member)?|recruiters?|recruiting\s+team|hiring\s+team|" \
                 r"talent\s+team|hr(?:\s+team)?)\s+will\s+(?:get\s+back|contact|review|reach\s+out|be\s+in\s+touch)|submission\s+(?:was\s+)?(?:successful|received)|" \
                 r"thank(?:s|\s+you)\s+for\s+(?:your\s+(?:message|submission|response|enquiry|inquiry|time)|contacting\s+us|reaching\s+out|" \
                 r"getting\s+in\s+touch|submitting)|(?:your\s+)?message\s+(?:has\s+been|was)\s+(?:successfully\s+)?sent|" \
                 r"it\s+has\s+been\s+sent|we(?:'ve|\s+have)\s+received\s+your\s+(?:message|submission|details|enquiry|inquiry|request|" \
                 r"information)|new\s+submission\s+added|(?:one\s+of\s+)?our\s+(?:consultants?|representatives?|executives?|experts?)\s+" \
                 r"will\s+(?:get\s+back|contact|reach\s+out|be\s+in\s+touch)"
FAILURE = (r"failed\s+to\s+(?:submit|send|apply|process)|submission\s+(?:has\s+)?failed|(?:could\s+not|couldn'?t|unable\s+to)\s+"
           r"(?:submit|send|process|save)\s+(?:your\s+|the\s+)?(?:application|form|request|message)|error\s+(?:trying|while|when)\s+"
           r"(?:to\s+)?(?:send|submit|process)|something\s+went\s+wrong|an?\s+(?:unexpected\s+)?error\s+(?:has\s+)?occurred|"
           r"please\s+try\s+again\s+later|application\s+(?:could\s+not|was\s+not)\s+(?:be\s+)?(?:submitted|sent)")
REFERENCE_ID = (r"(?:application|reference|confirmation|candidate|submission|ticket)\s*(?:id|no\.?|number|#|code)\s*(?:is\s*)?[:#]?"
                r"\s*([A-Z0-9][A-Z0-9-]{3,})")
# a short message that is only a reference ("Reference: YA-01591"): supporting (never "Job reference: ...", anchored)
REFERENCE_SHOWN = r"^\s*(?:your\s+)?(?:reference|ref\.?|tracking\s+(?:id|number))\s*(?:#\s*:?|:)\s*[A-Z]{0,6}-?\d{3,}[A-Z0-9-]*\s*\.?$"
CONFIRMATION_URL = re.compile(r"thank[-_]?you|thanks|/success|success[-_/=]|confirm(?:ation|ed)?\b|submitted|/applied|"
                              r"application[-_]?complete|/complete\b|/done\b", re.I)
SERVER_ERROR = re.compile(r"^\s*(?:5\d\d\s+)?(?:bad\s+gateway|service\s+(?:temporarily\s+)?unavailable|internal\s+server\s+"
                          r"error|gateway\s+time-?out)|^\s*5\d\d\s+[a-z]+(?:\s+[a-z]+)?\b", re.I)
_ANALYTICS = re.compile(r"google-analytics|googletagmanager|doubleclick|facebook\.(?:com|net)|hotjar|clarity\.ms|segment\.|"
                        # advertising / conversion pixels and bot-detection telemetry fire on any click: never a submission
                        r"google\.[a-z.]+/(?:pagead|rmkt|ads|ccm)/|googleadservices|googlesyndication|datadome\.co|perimeterx|"
                        r"px-cdn|bing\.com/action|ads-twitter|adsrvr\.org|"
                        r"sentry|newrelic|nr-data|mixpanel|amplitude|intercom|hubspot\.com/(?:__ptq|collect)|linkedin\.com/(?:li/|"
                        r"collect)|bat\.bing|tiktok|snap\.licdn|stats\.|/analytics|/track(?:ing)?\b|/log(?:s|ging)?\b|/beacon",
                        re.I)
_CAPTCHA_NET = re.compile(r"hcaptcha\.com/(?:getcaptcha|checkcaptcha)|recaptcha/(?:api2|enterprise)/(?:reload|userverify)|"
                          r"challenges\.cloudflare\.com|arkoselabs|funcaptcha|captcha-delivery", re.I)
# ("mail_sent": WordPress Contact Form 7, behind many small-company application forms)
_JSON_SUCCESS = re.compile(r'"(?:success|ok)"\s*:\s*true|"status"\s*:\s*"(?:success|ok|submitted|applied|created|received|'
                           r'mail_sent)"', re.I)
_JSON_APP_ID = re.compile(r'"(?:application_?id|applicationId|candidate_?id|candidateId|submission_?id|submissionId|'
                          r'reference(?:_?id|Id)?)"\s*:\s*(?:"(?=[\w-]*\d)[\w-]{3,}"|\d{3,})', re.I)  # an id, not null/"spam"
_JSON_FAILURE = re.compile(r'"(?:success|ok)"\s*:\s*false|"error"\s*:\s*(?!null|false|""|\{\s*\})|"errors"\s*:\s*\[\s*[{"]|'
                           r'"status"\s*:\s*"(?:validation_failed|mail_failed|spam|aborted|acceptance_missing|failed|failure|'
                           r'error)"', re.I)

_SNAPSHOT_JS = r"""
(args) => {
  const [srcs, submitMark] = args;
  const rx = Object.fromEntries(Object.entries(srcs).map(([k, v]) => [k, new RegExp(v, 'i')]));
  const Q = (t) => t.replace(/[\u2018\u2019\u02BC]/g, "'");  // "we’ve" reads as "we've"
  const T = (e) => Q((e && (e.innerText || e.textContent) || '').replace(/\s+/g, ' ').trim());
  const visible = (e) => !!e && e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden';
  const DESC = '[class*="description" i], [id*="description" i], [class*="job-desc" i], [id*="job-desc" i], ' +
               '[class*="jd-" i], [data-view-name*="description" i], [class*="job-detail" i], [class*="jobdetail" i]';
  const region = (e) => {
    if (e.closest('dialog[open], [role=dialog], [role=alertdialog], [aria-modal=true]')) return 'dialog';
    const box = e.closest('[class*="modal" i], [class*="popup" i], [class*="lightbox" i]');  // custom modals
    if (box && box !== document.body && box !== document.documentElement) return 'dialog';
    if (e.closest('[role=alert], [role=status], [aria-live=polite], [aria-live=assertive]')) return 'alert';
    if (e.closest('[class*="toast" i], [class*="snackbar" i], [class*="notification" i], [class*="flash" i]')) return 'toast';
    if (e.closest('h1, h2, h3, h4')) return 'heading';
    if (e.closest('[class*="success" i], [class*="confirm" i], [class*="thank" i], [id*="success" i], [id*="confirm" i], [id*="thank" i]')) return 'success-box';
    if (e.closest('[class*="error" i], [class*="fail" i], [class*="danger" i], [class*="invalid" i]')) return 'error-box';
    return 'page';
  };
  const messages = []; const seen = new Set();
  const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(), k = 0; n && k < 20000; n = walker.nextNode(), k++) {
    const raw = Q((n.nodeValue || '').replace(/\s+/g, ' ').trim());
    if (raw.length < 4) continue;
    let el = n.parentElement;
    if (!el || /^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE|OPTION)$/.test(el.tagName) || !visible(el)) continue;
    while (el.parentElement && getComputedStyle(el).display === 'inline') el = el.parentElement;
    const block = T(el);
    const text = block.length <= 300 ? block : raw;
    const kinds = Object.keys(rx).filter(k2 => rx[k2].test(text));
    if (!kinds.length || seen.has(text)) continue;
    seen.add(text);
    messages.push({ text: text.slice(0, 300), kinds, region: region(el), long: block.length > 300, inDescription: !!el.closest(DESC) });
    if (messages.length > 40) break;
  }
  const fields = Array.from(document.querySelectorAll('input, select, textarea')).filter(e =>
      !['hidden', 'submit', 'button', 'image', 'reset'].includes((e.type || '').toLowerCase()) && visible(e))
    .map(e => e.name || e.id || (e.labels && e.labels[0] && T(e.labels[0])) || e.getAttribute('aria-label') ||
              e.getAttribute('placeholder') || e.dataset.pjaId || '').filter(Boolean);  // stable across re-renders
  const submit = submitMark ? document.querySelector('[data-pja-submit="' + submitMark + '"]') : null;
  return {
    url: location.href, title: document.title || '', messages, fields,
    submitVisible: !!submit && visible(submit), submitEnabled: !!submit && !submit.disabled && submit.getAttribute('aria-disabled') !== 'true',
    bodyStart: T(document.body).slice(0, 300),
  };
}
"""


@dataclass
class Signal:
    name: str
    strength: str  # strong | supporting | failure | refusal | info
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.name}: {self.detail}" if self.detail else self.name


@dataclass
class PageState:
    url: str = ""
    title: str = ""
    messages: list[dict] = field(default_factory=list)
    fields: list[str] = field(default_factory=list)
    submit_visible: bool = False
    submit_enabled: bool = False
    body_start: str = ""
    errors: list[str] = field(default_factory=list)

    @classmethod
    def from_js(cls, d: dict, errors: list[str] | None = None) -> PageState:
        return cls(url=d.get("url") or "", title=d.get("title") or "", messages=d.get("messages") or [],
                   fields=d.get("fields") or [], submit_visible=bool(d.get("submitVisible")),
                   submit_enabled=bool(d.get("submitEnabled")), body_start=d.get("bodyStart") or "",
                   errors=list(errors or []))


@dataclass
class NetworkHit:
    method: str
    url: str
    status: int
    success: bool = False
    app_id: bool = False
    failure: bool = False
    message: str = ""  # the site's own words in a JSON answer ("message" / "error" ...), for precise reasons
    later_page: bool = False  # sent by the page the site went to afterwards, not by the form: says nothing about it


@dataclass
class Verdict:
    state: SubmissionState
    signals: list[Signal] = field(default_factory=list)
    confirmation_text: str = ""
    message: str = ""

    @property
    def evidence(self) -> list[str]:
        return [str(s) for s in self.signals]

    @property
    def verification(self) -> str:
        return {SubmissionState.CONFIRMED: "confirmed", SubmissionState.FAILED: "failed",
                SubmissionState.UNCERTAIN: "uncertain"}.get(self.state, "")


_REGEX_SOURCES = {"success": APPLICATION_SUCCESS, "thanks": GENERIC_THANKS, "failure": FAILURE, "reference": REFERENCE_ID,
                  "reference_shown": REFERENCE_SHOWN}
_CONFIRM_REGIONS = {"dialog", "alert", "toast", "heading", "success-box", "error-box"}  # message areas, not body text
_NOT_AN_ERROR = re.compile(f"{APPLICATION_SUCCESS}|{GENERIC_THANKS}", re.I)


def _new_messages(before: PageState, after: PageState) -> list[dict]:
    old = {m["text"] for m in before.messages}
    return [m for m in after.messages if m["text"] not in old]


# what a site says after ANY message form ("Thank you for your inquiry", "your message has been sent"): it
# acknowledges an application only when a résumé went with the form
CONTACT_ACK = re.compile(r"\b(?:inquiry|enquiry|message|contacting|reaching\s+out|getting\s+in\s+touch|consultants?|"
                         r"representatives?|executives?|experts?)\b", re.I)


# The page a site shows when it wants you to sign in (intellohire, run #41: after "Save" it went to "Login" - phone
# number and "Get OTP"). Landing there after a submit is a request to sign in, never the confirmation of it.
_SIGN_IN_TITLE = re.compile(r"^\W*(?:log\s?in|sign\s?in|sign\s?up|register|create\s+(?:an\s+)?account)\b", re.I)
_SIGN_IN_PATH = re.compile(r"/(?:log-?in|sign-?in|sign-?up|sso|auth(?:enticate)?|users/sign_in|account/login|register)"
                           r"(?:[/.]|$)", re.I)
_SIGN_IN_BODY = re.compile(r"\b(?:get|send|resend|enter|verify)\s+(?:the\s+|your\s+)?otp\b|\blog\s?in\s*/\s*sign\s?up\b|"
                           r"\bsign\s?in\s+to\s+(?:continue|your\s+account)|\bforgot\s+(?:your\s+)?password", re.I)


def sign_in_page(state: PageState) -> bool:
    """The page is a sign-in / sign-up page (by its title, its address or what it asks for)."""
    return bool(_SIGN_IN_TITLE.search(state.title) or _SIGN_IN_PATH.search(urlparse(state.url).path)
                or _SIGN_IN_BODY.search(state.body_start))


def decide(before: PageState, after: PageState, *, network: list[NetworkHit] | None = None, portal: str = "",
           final: bool = False, elapsed: float = 0.0, resume_attached: bool = True) -> Verdict:
    """The verdict from what changed between the moment before the click and now (pure: unit tested).

    ``resume_attached=False``: the submitted form carried no résumé / document. Then the wording of a contact form
    ("Thank you for your message / inquiry") is never taken as the confirmation of a job application."""
    network = network or []
    signals: list[Signal] = []
    if portal:
        signals.append(Signal("portal_confirmation", "strong", portal[:200]))
        return Verdict(SubmissionState.CONFIRMED, signals, confirmation_text=portal[:300])
    fresh = [m for m in _new_messages(before, after) if not m.get("inDescription") and not m.get("long")]
    success = [m for m in fresh if "success" in m["kinds"]]
    strong_success = [m for m in success if m["region"] in _CONFIRM_REGIONS]  # page text alone never confirms
    reference = [m for m in fresh if "reference" in m["kinds"]]
    failure = [m for m in fresh if "failure" in m["kinds"]]
    thanks = [m for m in fresh if "thanks" in m["kinds"] and m["region"] in _CONFIRM_REGIONS | {"page"}
              and (resume_attached or not CONTACT_ACK.search(m["text"]))]
    contact_only = [m for m in fresh if "thanks" in m["kinds"] and CONTACT_ACK.search(m["text"])] if not resume_attached else []
    form_gone = bool(before.fields) and not (set(before.fields) & set(after.fields))
    submit_gone = before.submit_visible and not after.submit_visible
    url_changed = after.url.split("#")[0] != before.url.split("#")[0]
    confirm_url = url_changed and bool(CONFIRMATION_URL.search(urlparse(after.url).path + "?" + urlparse(after.url).query))
    # (what the next page asks its own servers - a login page's start-up calls, a dashboard's data - is not the
    # answer to the form: intellohire's login page got {"success": true} from its own backend)
    later = [h for h in network if h.later_page]
    network = [h for h in network if not h.later_page]
    net_ok = [h for h in network if 200 <= h.status < 300 and (h.success or h.app_id)]
    net_app_id = [h for h in net_ok if h.app_id and h.success]
    net_fail = [h for h in network if h.status >= 400 or h.failure]
    signed_out = url_changed and not confirm_url and sign_in_page(after) and not sign_in_page(before)

    for m in strong_success:
        signals.append(Signal("confirmation_message", "strong", f"{m['text'][:160]} ({m['region']})"))
    for m in reference:
        signals.append(Signal("reference_id", "strong", m["text"][:160]))
    for h in net_app_id:
        signals.append(Signal("submit_request_succeeded", "strong", f"{h.method} {_short(h.url)} -> {h.status}, application id"))
    if any(s.strength == "strong" for s in signals):
        text = (strong_success or reference or [{"text": ""}])[0]["text"]
        return Verdict(SubmissionState.CONFIRMED, signals, confirmation_text=text or "Submission acknowledged by the site")

    if failure:
        signals.append(Signal("failure_message", "failure", failure[0]["text"][:160]))
        return Verdict(SubmissionState.FAILED, signals, message=f"The site reported a failure: \"{failure[0]['text'][:160]}\"")
    new_errors = [e for e in after.errors if e not in before.errors and not _NOT_AN_ERROR.search(e.replace("\u2019", "'"))]
    if new_errors and not form_gone:
        signals.append(Signal("validation_errors", "refusal", "; ".join(new_errors[:3])[:200]))
        return Verdict(SubmissionState.REFUSED, signals, message="; ".join(new_errors[:4]))

    # supporting evidence
    support: list[Signal] = []
    if confirm_url:
        support.append(Signal("confirmation_url", "supporting", after.url[:160]))
    for m in thanks + [m for m in success if m not in strong_success]:
        support.append(Signal("acknowledgement", "supporting", f"{m['text'][:120]} ({m['region']})"))
    for m in fresh:  # (on the same page, or a confirmation URL: another job's page may show its own reference)
        if "reference_shown" in m["kinds"] and "reference" not in m["kinds"] and (not url_changed or confirm_url):
            support.append(Signal("reference_shown", "supporting", m["text"][:120]))
    for h in net_ok:
        support.append(Signal("submit_request_accepted", "supporting", f"{h.method} {_short(h.url)} -> {h.status}"))
    for h in network:
        if h not in net_ok and not (h.status >= 400 or h.failure):
            support.append(Signal("submit_request_sent", "info", f"{h.method} {_short(h.url)} -> {h.status}"))
    if form_gone:
        support.append(Signal("form_gone", "supporting", "the application form is no longer on the page"))
    if submit_gone:
        support.append(Signal("submit_button_gone", "supporting", ""))
    for h in later[:3]:
        support.append(Signal("request_of_the_next_page", "info", f"{h.method} {_short(h.url)} -> {h.status}"))
    if signed_out:
        # the form is gone because the site now wants a sign-in: nothing here says the application was taken
        support.append(Signal("sign_in_page", "info", (after.title or after.url)[:80]))
        if not final:
            return Verdict(SubmissionState.WAITING_FOR_SUBMISSION_RESULT, support)
        sent = f"the form was sent ({network[0].method} {_short(network[0].url)} -> {network[0].status}), then " \
            if network else "after the submit "
        return Verdict(SubmissionState.UNCERTAIN, support,
                       message=f"{sent}the site showed its sign-in page (\"{(after.title or 'sign in')[:60]}\") instead of a "
                               "confirmation - it may want you to sign in or enter a code before the application counts")
    kinds = {s.name for s in support if s.strength == "supporting"}
    content = kinds & {"acknowledgement", "submit_request_accepted", "reference_shown"}
    if content and len(kinds) >= 2:
        text = next((s.detail for s in support if s.name == "acknowledgement"), "") or "Submission accepted by the site"
        return Verdict(SubmissionState.CONFIRMED, support, confirmation_text=text.rsplit(" (", 1)[0])
    if net_fail and not form_gone:
        support.append(Signal("submit_request_failed", "failure", f"{net_fail[0].method} {_short(net_fail[0].url)} -> "
                                                                   f"{net_fail[0].status}"))
        if final:
            said = f': "{net_fail[0].message[:160]}"' if net_fail[0].message else ""
            return Verdict(SubmissionState.FAILED, support, message=f"The site answered the submission with an error "
                                                                    f"({net_fail[0].status}){said}")
    if SERVER_ERROR.search(after.body_start) and url_changed | (after.body_start != before.body_start):
        support.append(Signal("server_error_page", "info", after.body_start[:80]))
        return Verdict(SubmissionState.UNCERTAIN, support,
                       message=f"The site returned an error page ('{SERVER_ERROR.search(after.body_start).group(0).strip()}') "
                               "right after submitting - it may not have received the application")
    new_fields = set(after.fields) - set(before.fields)
    if after.fields and new_fields and (form_gone or len(new_fields) >= 2) and elapsed >= 2:
        return Verdict(SubmissionState.NEXT_STEP, support + [Signal("new_form_step", "info", f"{len(new_fields)} new field(s)")])
    if not final:
        return Verdict(SubmissionState.WAITING_FOR_SUBMISSION_RESULT, support)
    if (not url_changed and not network and not form_gone and after.submit_visible and after.submit_enabled
            and after.fields == before.fields and not _new_messages(before, after)):
        return Verdict(SubmissionState.NO_EFFECT, support, message="the click did not trigger anything on the page")
    what = ", ".join(str(s) for s in support) or "no change the assistant could read"
    if contact_only:
        what = (f"the site acknowledged a message, not an application (\"{contact_only[0]['text'][:120]}\"), and no "
                f"résumé went with the form; {what}")
    if not support and network:
        what = f"a request was sent ({network[0].method} {_short(network[0].url)} -> {network[0].status}) but the page " \
               "showed no confirmation"
    return Verdict(SubmissionState.UNCERTAIN, support, message=what)


_MESSAGE_KEYS = ("message", "error", "errorMessage", "error_message", "statusMessage", "description", "detail", "msg",
                 "reason", "validationError")


def _json_message(body: str) -> str:
    """The human-readable message of a JSON answer (first of the usual keys, nested one level), '' if none."""
    try:
        data = json.loads(body)
    except ValueError:
        return ""

    def find(node: object, depth: int = 0) -> str:
        if isinstance(node, dict):
            for key in _MESSAGE_KEYS:
                value = node.get(key)
                if isinstance(value, str) and 2 < len(value.strip()) <= 400:
                    return value.strip()
            if depth < 2:
                for value in node.values():
                    found = find(value, depth + 1)
                    if found:
                        return found
        elif isinstance(node, list) and depth < 2:
            for value in node[:5]:
                found = find(value, depth + 1)
                if found:
                    return found
        return ""

    return re.sub(r"\s+", " ", find(data))[:300]


def _short(url: str) -> str:
    u = urlparse(url)
    return (u.netloc + u.path)[:80]


def _page_key(url: str) -> str:
    """Host + path of a document ('' for blank / data pages): same key = same page, whatever its query or hash."""
    u = urlparse(url or "")
    return f"{u.netloc.lower()}{u.path.rstrip('/')}" if u.scheme in {"http", "https"} else ""


class SubmissionWatcher:
    """Capture the page before the submit click, watch the network, then wait for a verdict."""

    def __init__(self, page: Page, *, frame: Frame | None = None, submit_selector: str = "",
                 portal_probe: Callable[[], Awaitable[str]] | None = None,
                 error_probe: Callable[[], Awaitable[list[str]]] | None = None, resume_attached: bool = True) -> None:
        self.resume_attached = resume_attached  # False: a contact form's acknowledgement confirms nothing
        self.page = page
        self.frame = frame or page.main_frame
        self.submit_selector = submit_selector
        self.portal_probe = portal_probe
        self.error_probe = error_probe
        self.before = PageState()
        self.network: list[NetworkHit] = []
        self.mutating_requests = 0
        self.captcha_requests = 0
        self._inflight: set[int] = set()  # submit requests the site has not answered yet
        self._origins: set[str] = set()  # the page (and frame) the form is on
        self._later: set[int] = set()  # requests sent by a page the site went to afterwards
        self.state = SubmissionState.SUBMIT_CLICKED
        self._mark = ""
        self._tasks: list[asyncio.Task] = []

    async def begin(self) -> None:
        if self.submit_selector:
            self._mark = str(id(self))
            try:
                await self.frame.locator(self.submit_selector).first.evaluate(
                    "(el, m) => el.setAttribute('data-pja-submit', m)", self._mark)
            except PlaywrightError:
                self._mark = ""
        self.before = await self.snapshot(self.page)
        self._origins = {k for k in (_page_key(self.page.url), _page_key(self.frame.url)) if k}
        self.page.on("request", self._on_request)
        self.page.on("response", self._on_response)
        self.page.on("requestfailed", self._on_request_failed)

    def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        for name, fn in (("request", self._on_request), ("response", self._on_response),
                         ("requestfailed", self._on_request_failed)):
            try:
                self.page.remove_listener(name, fn)
            except Exception:  # noqa: BLE001 - already detached
                pass

    def _relevant(self, request: Request) -> bool:
        return request.method in {"POST", "PUT", "PATCH"} and not _ANALYTICS.search(request.url) and \
            not _CAPTCHA_NET.search(request.url) and request.resource_type in {"xhr", "fetch", "document", "other"}

    def _on_request(self, request: Request) -> None:
        if _CAPTCHA_NET.search(request.url):
            self.captcha_requests += 1  # a challenge was issued, not our submission
        elif self._relevant(request):
            self.mutating_requests += 1
            self._inflight.add(id(request))
            if self._from_later_page(request):
                self._later.add(id(request))

    def _from_later_page(self, request: Request) -> bool:
        """The request comes from a document other than the form's: the page the site went to after the submit.
        (The form's own POST that navigates the page is still the form's.)"""
        try:
            if request.is_navigation_request():
                return False
            at = _page_key(request.frame.url)
        except PlaywrightError:
            return False
        return bool(at) and bool(self._origins) and at not in self._origins

    def _on_request_failed(self, request: Request) -> None:
        self._inflight.discard(id(request))

    def _on_response(self, response: Response) -> None:
        self._inflight.discard(id(response.request))
        if self._relevant(response.request):
            self._tasks.append(asyncio.ensure_future(self._record(response)))

    async def _record(self, response: Response) -> None:
        hit = NetworkHit(response.request.method, response.url, response.status,
                         later_page=id(response.request) in self._later)
        try:
            ctype = (await asyncio.wait_for(response.header_value("content-type"), 3)) or ""
            if "json" in ctype or "x-component" in ctype:  # (x-component: a Next.js server action's reply)
                # a response of a page we navigated away from may never deliver its body: never wait on it
                body = (await asyncio.wait_for(response.text(), 3))[:20000]
                hit.success = bool(_JSON_SUCCESS.search(body))
                hit.app_id = bool(_JSON_APP_ID.search(body))
                hit.failure = bool(_JSON_FAILURE.search(body)) and not hit.success
                hit.message = _json_message(body)
        except (PlaywrightError, asyncio.TimeoutError):
            pass
        self.network.append(hit)

    async def snapshot(self, page: Page) -> PageState:
        from app.browser.forms import collect_errors

        try:
            data = await page.main_frame.evaluate(_SNAPSHOT_JS, [_REGEX_SOURCES, self._mark])
        except PlaywrightError:
            return PageState(url=page.url if not page.is_closed() else "")
        if self.frame is not page.main_frame:
            try:  # the form lives in an iframe: its fields and messages count too
                sub = await self.frame.evaluate(_SNAPSHOT_JS, [_REGEX_SOURCES, self._mark])
                data["messages"] = (data.get("messages") or []) + (sub.get("messages") or [])
                data["fields"] = (data.get("fields") or []) + (sub.get("fields") or [])
                data["submitVisible"] = data.get("submitVisible") or sub.get("submitVisible")
                data["submitEnabled"] = data.get("submitEnabled") or sub.get("submitEnabled")
            except PlaywrightError:
                pass
        errors = await self.error_probe() if self.error_probe else await collect_errors(self.frame)
        return PageState.from_js(data, errors)

    async def current(self, page: Page | None = None, *, final: bool = False, elapsed: float = 0.0) -> Verdict:
        page = page or self.page
        portal = ""
        if self.portal_probe is not None:
            try:
                portal = await self.portal_probe()
            except Exception as exc:  # noqa: BLE001 - a probe never breaks verification
                log.debug("portal probe failed: %s", exc)
        after = await self.snapshot(page)
        if self._tasks:  # give pending response reads a moment, never block the verdict on them
            _done, pending = await asyncio.wait(self._tasks, timeout=1.5)
            self._tasks = list(pending)
        net = list(self.network)
        if not net and self.mutating_requests:
            net = []  # requests without a readable answer yet
        verdict = decide(self.before, after, network=net, portal=portal, final=final, elapsed=elapsed,
                         resume_attached=self.resume_attached)
        if self.captcha_requests and verdict.state not in {SubmissionState.CONFIRMED, SubmissionState.FAILED}:
            verdict = Verdict(SubmissionState.BLOCKED, verdict.signals + [Signal("captcha_challenge", "info",
                              "a CAPTCHA challenge was requested after the click")],
                              message="a CAPTCHA challenge appeared after submitting - only you can solve it")
        if verdict.state == SubmissionState.NO_EFFECT and self.mutating_requests:
            verdict = Verdict(SubmissionState.UNCERTAIN, verdict.signals + [Signal("request_sent", "info", "")],
                              message="a request was sent; the page showed no confirmation")
        self.state = verdict.state
        return verdict

    async def wait(self, page_fn: Callable[[], Page] | None = None, *, wait_s: float = 25.0,
                   settle_s: float = 2.0) -> Verdict:
        """Poll until a decisive verdict (CONFIRMED / FAILED / REFUSED / NEXT_STEP) or the wait ends."""
        self.state = SubmissionState.WAITING_FOR_SUBMISSION_RESULT
        loop = asyncio.get_running_loop()
        started = loop.time()
        verdict = Verdict(SubmissionState.WAITING_FOR_SUBMISSION_RESULT)
        limit = wait_s
        while True:
            if loop.time() - started >= limit:
                if self._inflight and limit == wait_s:  # the site is still processing the submit ("Submitting...")
                    limit = wait_s + _SLOW_SUBMIT_EXTRA_S
                    continue
                break
            await asyncio.sleep(0.5)
            page = page_fn() if page_fn else self.page
            if page.is_closed():
                break
            elapsed = loop.time() - started
            verdict = await self.current(page, elapsed=elapsed)
            if verdict.state in {SubmissionState.CONFIRMED, SubmissionState.FAILED, SubmissionState.BLOCKED}:
                return verdict
            if verdict.state in {SubmissionState.REFUSED, SubmissionState.NEXT_STEP} and elapsed >= settle_s:
                return verdict
        page = page_fn() if page_fn else self.page
        if page.is_closed():
            return Verdict(SubmissionState.UNCERTAIN, verdict.signals, message="the page was closed before a result")
        final = await self.current(page, final=True, elapsed=loop.time() - started)
        if final.state == SubmissionState.UNCERTAIN and self._inflight:
            final.message = (f"the site had not answered the submit request after {round(loop.time() - started)} s"
                             + (f"; {final.message}" if final.message else ""))
        return final


_SLOW_SUBMIT_EXTRA_S = 35.0  # extra wait while a submit request is still unanswered (slow uploads, busy servers)


async def page_state(page: Page) -> PageState:
    """The page as the watcher sees it (for flows whose 'submit' is not a single form button, e.g. Naukri)."""
    return await SubmissionWatcher(page).snapshot(page)


async def confirmation_since(page: Page, before: PageState) -> str:
    """A confirmation message that appeared after ``before`` in a confirmation region ('' when there is none).
    Text that was already on the page (a job description saying "application submitted") never counts."""
    after = await page_state(page)
    for m in _new_messages(before, after):
        if "success" in m["kinds"] and m["region"] in _CONFIRM_REGIONS and not m.get("inDescription") and not m.get("long"):
            return m["text"]
    return ""


async def verify_page(engine, url: str) -> tuple[str, list[str]]:
    """Open the page an earlier submit ended on (in a separate tab) and look for a confirmation that is there now:
    a confirmation message in a confirmation region together with a confirmation URL or the absence of the form.
    Returns (confirmation text, evidence) - ('', evidence) when there is none."""
    from app.security import is_web_url

    if not is_web_url(url):  # the URL was recorded from a page: only web addresses are opened again
        return "", ["the recorded address is not a web page - nothing was opened"]
    page = await engine.context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(2500)
        state = await page_state(page)
        evidence = [f"opened {url[:120]}"]
        confirm_url = bool(CONFIRMATION_URL.search(urlparse(page.url).path))
        for m in state.messages:
            if "success" in m["kinds"] and m["region"] in _CONFIRM_REGIONS and not m.get("inDescription"):
                if confirm_url or not state.fields:
                    evidence.append(f"confirmation_message: {m['text'][:160]}")
                    return m["text"], evidence
        if confirm_url and any("thanks" in m["kinds"] for m in state.messages) and not state.fields:
            evidence.append(f"confirmation_url: {page.url[:120]}")
            return f"Confirmation page: {page.url[:160]}", evidence
        evidence.append("no confirmation on the page now")
        return "", evidence
    except PlaywrightError as exc:
        return "", [f"could not open {url[:120]}: {str(exc).splitlines()[0][:120]}"]
    finally:
        try:
            await page.close()
        except PlaywrightError:
            pass
