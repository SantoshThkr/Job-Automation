"""Pure text classification used by the navigator: what a button does and what a page shows.

Kept free of Playwright so it can be unit-tested exhaustively. The navigator combines these
signals with page structure (forms, dialogs, password fields) to decide what to do next.
"""

from __future__ import annotations

import re
from enum import StrEnum

from app.portals.common import EXPIRED_TEXT, SUCCESS_TEXT


class Kind(StrEnum):
    APPLY = "apply"  # opens the application (job page CTA)
    NEXT = "next"  # moves to the next step of a multi-step form
    SUBMIT = "submit"  # irreversible: sends the application
    GUEST = "guest"  # continue without an account
    SIGNIN = "signin"
    SIGNUP = "signup"
    SSO = "sso"  # sign in with Google/LinkedIn/...: never used automatically
    COOKIES = "cookies"  # accept a cookie / consent banner
    CLOSE = "close"  # dismiss a popup / overlay
    UPLOAD = "upload"  # opens a file chooser for the resume
    BACK = "back"
    DANGER = "danger"  # never click (logout, withdraw, delete, share, save job...)
    OTHER = "other"


def _rx(*parts: str) -> re.Pattern[str]:
    return re.compile("|".join(parts), re.I)


_ARROWS = r"\s*[!»›>→⟶➔→]*\s*"

APPLY_RX = _rx(
    rf"^(?:quick\s+|easy\s+|1[\s-]?click\s+|one[\s-]click\s+)?apply(?:\s+(?:now|online|here|today))?{_ARROWS}$",
    r"^apply\s+(?:for|to)\s+(?:this\s+|the\s+)?(?:job|position|role|opening|vacancy|opportunity|post)\b",
    r"^apply\s+for\s+.{3,80}$",
    r"^apply\s+on\s+(?:the\s+)?(?:company|employer|corporate)\s+(?:site|website|page)",
    r"^apply\s+(?:with|using)\s+(?:my\s+|your\s+)?(?:resume|cv)$",
    r"^(?:start|begin|continue)\s+(?:your\s+|my\s+)?application$",
    r"^i'?m\s+interested$|^i\s+am\s+interested$|^interested$",
    r"^submit\s+(?:your\s+|my\s+)?(?:resume|cv|profile|interest)$",
    r"^send\s+(?:your\s+|my\s+)?(?:resume|cv)$",
    r"^(?:apply|application)\s*form$",
)
APPLY_AVOID_RX = _rx(
    r"apply\s+(?:with|using|via|through)\s+(?:linked\s*in|indeed|seek|google|facebook|xing|glassdoor|naukri|monster)",
    r"\bapplied\b", r"how\s+to\s+apply", r"apply\s+later", r"\bfilters?\b", r"apply\s+(?:filter|changes)",
    r"apply\s+(?:coupon|code|promo)",
)
NEXT_RX = _rx(
    rf"^(?:next|continue|proceed|go\s+on){_ARROWS}$",
    rf"^(?:save\s*(?:&|and)\s*)?(?:continue|next|proceed){_ARROWS}$",
    rf"^next\s+(?:step|page|section|question){_ARROWS}$",
    rf"^continue\s+(?:to\s+.{{1,40}}|application|applying|with\s+application){_ARROWS}$",
    rf"^(?:review|preview)(?:\s+(?:application|my\s+application|your\s+application|details))?{_ARROWS}$",
    r"^go\s+to\s+(?:next|review|step).{0,30}$",
    rf"^save\s+(?:and\s+|&\s+)?next{_ARROWS}$",
    r"^(?:ok|okay),?\s+(?:continue|next)$",
)
# Anything that may send the application. Classified as SUBMIT only in a form context for bare "apply".
SUBMIT_RX = _rx(
    r"\bsubmit\b",
    r"^send(?:\s+(?:my\s+|your\s+|the\s+)?(?:application|applications|details|profile))?\s*$",
    r"^(?:finish|complete)(?:\s+(?:application|my\s+application|&\s+submit|and\s+submit|applying))?$",
    r"^confirm(?:\s+(?:and|&)\s+(?:submit|apply|send))?(?:\s+application)?$",
    r"^(?:done|finish\s+application)$",
    r"^apply(?:\s+now)?$",
    r"^send\s+application$",
)
GUEST_RX = _rx(
    r"(?:apply|continue|proceed)\s+(?:as\s+(?:a\s+)?guest|without\s+(?:an?\s+|creating\s+an?\s+)?(?:account|profile|sign(?:ing)?[\s-]*in|registration|registering|log(?:ging)?[\s-]*in))",
    r"guest\s+(?:apply|application|user)", r"^(?:continue|proceed)\s+as\s+(?:a\s+)?guest$", r"^apply\s+manually$",
    r"skip\s+(?:sign[\s-]*in|login|log\s+in|registration|this\s+step)", r"no,?\s+thanks?,?\s+(?:just\s+)?apply",
    r"^use\s+my\s+last\s+application$",
)
SSO_RX = _rx(
    r"(?:sign|log)\s*[\s-]?(?:in|up)\s+(?:with|using|via)\s+(?:google|linked\s*in|facebook|apple|microsoft|github|indeed|seek|twitter|x\b)",
    r"continue\s+with\s+(?:google|linked\s*in|facebook|apple|microsoft|github|indeed)",
    r"^(?:google|linkedin|facebook|apple|microsoft)$",
)
SIGNIN_RX = _rx(
    r"^(?:sign|log)\s*[\s-]?in(?:\s+(?:to\s+(?:apply|continue|your\s+account)|now|here|with\s+(?:e-?mail|password)))?$",
    r"^login$", r"^(?:existing|returning)\s+(?:user|candidate)", r"^already\s+have\s+an\s+account",
    r"^(?:sign|log)\s*in\s+to\s+.{1,40}$",
)
SIGNUP_RX = _rx(
    r"^(?:create|register)\s+(?:an?\s+|new\s+|your\s+|my\s+)?(?:account|profile)$",
    r"^(?:sign|register)\s*[\s-]?up(?:\s+(?:now|here|to\s+apply))?$", r"^register(?:\s+now)?$",
    r"^create\s+(?:an?\s+)?account\s+(?:and|&|to)\s+(?:apply|continue)", r"^new\s+(?:user|candidate)",
    r"^(?:join|join\s+now)$", r"don'?t\s+have\s+an\s+account",
)
COOKIES_RX = _rx(
    r"^(?:accept|allow)(?:\s+(?:all|all\s+cookies|cookies|necessary|essential|only\s+necessary|and\s+(?:close|continue)|&\s+(?:close|continue)))?(?:\s+cookies)?$",
    r"^i\s+(?:accept|agree|understand|consent)$", r"^(?:agree|consent)(?:\s+(?:and|&)\s+(?:close|continue|proceed))?$",
    r"^(?:got\s+it|ok(?:ay)?|understood|accept\s+&\s+continue|reject\s+all|decline\s+all|necessary\s+only|essential\s+only)!?$",
)
CLOSE_RX = _rx(
    r"^(?:close|dismiss|×|x|✕|✖|╳|no,?\s+thanks?|no\s+thank\s+you|not\s+now|maybe\s+later|skip(?:\s+for\s+now)?|"
    r"cancel|remind\s+me\s+later|not\s+interested|later|close\s+(?:dialog|modal|popup|window|banner))$",
)
UPLOAD_RX = _rx(
    r"^(?:upload|attach|add|choose|select|browse)(?:\s+(?:a|your|my|new))?\s+(?:resume|cv|file|document)s?(?:\s*/\s*(?:cv|resume))?",
    r"^(?:upload|attach|browse|choose\s+file|select\s+file)$", r"^(?:drop|drag).{0,30}(?:resume|cv|file)",
)
BACK_RX = _rx(r"^(?:back|previous|prev|go\s+back|‹\s*back|<\s*back|← back)(?:\s+(?:step|page))?$")
DANGER_RX = _rx(
    r"\blog\s*out\b|\bsign\s*out\b", r"\bwithdraw\b", r"\bdelete\b", r"\bremove\b", r"\bunsubscribe\b",
    r"^(?:save|saved)(?:\s+(?:job|this\s+job|for\s+later|search))?$", r"^share\b", r"\breport\b", r"^print\b",
    r"\brefer\s+(?:a\s+)?friend\b", r"^(?:create\s+)?(?:job\s+)?alert", r"\bdownload\b", r"\bfollow\b",
    r"^(?:edit|change)\s+(?:profile|password)", r"\bmessage\s+(?:the\s+)?recruiter\b", r"^discard\b",
    r"^reset\b", r"^clear(?:\s+all)?$", r"\bfeedback\b", r"^chat\b", r"^similar\s+jobs",
    # paid upsells: never part of applying
    r"\b(?:go|try|get|buy|upgrade(?:\s+to)?)\s+premium\b", r"\bbecome\s+a\s+pro\b", r"\bpower\s+up\b", r"^upgrade\b",
    r"\bbuy\s+now\b", r"^pricing$", r"naukri\s*360", r"\bpro\s+(?:plan|membership|subscription)\b",
    # policy / legal pages: reading material, never the next step of an application
    r"know\s+your\s+rights", r"^eeo\b|\beeo\s+(?:poster|is\s+the\s+law)", r"equal\s+(?:employment\s+)?opportunity\s+(?:is\s+the\s+law|poster)",
    r"^(?:privacy|cookie)\s+(?:policy|notice|statement|settings)$", r"^terms\s+(?:of\s+(?:use|service)|and\s+conditions|&\s+conditions)$",
    r"^accessibility\s+statement$",
)
# sales / contact / newsletter forms are not job applications (their buttons are never clicked)
CONTACT_BUTTON_RX = _rx(
    r"^send\s+(?:a\s+|us\s+a\s+|your\s+)?(?:message|enquiry|inquiry)$", r"^let'?s\s+talk\b", r"^talk\s+to\s+(?:us|sales|an?\s+expert)$",
    r"^(?:contact|contact\s+us|contact\s+sales|get\s+in\s+touch)$", r"^request\s+(?:a\s+)?(?:demo|quote|call(?:\s*back)?|proposal)$",
    r"^book\s+(?:a\s+)?(?:demo|call|meeting|consultation)$", r"^subscribe(?:\s+now)?$", r"^notify\s+me$",
    r"^submit\s+(?:enquiry|inquiry|query|request)$", r"^(?:inquire|enquire|inquiry|enquiry)(?:\s+now)?$",
    r"^(?:get|request)\s+(?:a\s+|an\s+|your\s+)?(?:free\s+)?(?:quote|estimate|consultation|proposal|callback)$",
    r"^(?:hire|schedule\s+a\s+call|start\s+(?:a\s+|your\s+)?project)\b",
    # "Submit Audit Request", "Get a free quote", "Request consultation", "Book free demo"...
    r"^(?:submit|send|get|book|request|schedule)\s+(?:an?\s+|my\s+|your\s+|free\s+|the\s+)?(?:\w+\s+)?"
    r"(?:audit|demo|quote|estimate|consultation|proposal|callback|call\s*back|enquiry|inquiry|query|assessment)"
    r"(?:\s+request)?$",
)
# a job-search / filter form's button: never an application submit
SEARCH_BUTTON_RX = _rx(r"^(?:search|find|filter|apply\s+filters?|show\s+(?:jobs|results)|update\s+results)$",
                       r"^(?:search|find)\s+(?:jobs?|now|positions?|openings?|roles?|vacanc(?:y|ies)|careers?)$")

FORM_SAVE_RX = _rx(r"^save(?:\s+(?:application|my\s+application|details|and\s+submit|&\s+submit|and\s+apply|&\s+apply))?$")
COOKIE_CONTEXT_RX = re.compile(r"cookie|consent|gdpr|privacy\s+(?:preferences|settings|choices)|tracking\s+technologies", re.I)

ALREADY_APPLIED_TEXT = re.compile(
    r"you(?:'ve|\s+have)\s+already\s+applied|already\s+applied\s+(?:to|for)\s+this|"
    r"application\s+(?:for\s+this\s+(?:job|position)\s+)?already\s+(?:submitted|exists|received)|"
    r"you\s+applied\s+(?:on|\d+\s+(?:days?|hours?|weeks?)\s+ago)|duplicate\s+application", re.I)
VERIFY_EMAIL_TEXT = re.compile(
    r"verify\s+your\s+e-?mail|check\s+your\s+(?:e-?mail|inbox)|we(?:'ve|\s+have)\s+(?:just\s+)?sent\s+(?:you\s+)?"
    r"(?:an?\s+)?(?:e-?mail|verification|activation|confirmation)\s*(?:e-?mail|link|code)?|confirm\s+your\s+e-?mail|"
    r"activation\s+(?:link|e-?mail)|activate\s+your\s+account", re.I)
LOGIN_ERROR_TEXT = re.compile(
    r"(?:no|could\s+not\s+find\s+(?:an?|your))\s+account|account\s+(?:does\s+not|doesn'?t)\s+exist|"
    r"(?:invalid|incorrect|wrong)\s+(?:e-?mail|user\s*name|password|credentials|login)|not\s+(?:a\s+)?registered|"
    r"user\s+not\s+found|email\s+(?:is\s+)?not\s+recogni[sz]ed", re.I)
UNAVAILABLE_TEXT = re.compile(
    r"access\s+denied|you\s+(?:don'?t|do\s+not)\s+have\s+(?:access|permission)|(?:page|job|record|posting)\s+"
    r"(?:was\s+)?not\s+found|\b404\b|no\s+longer\s+exists|has\s+been\s+(?:removed|deleted|archived)|"
    r"link\s+(?:has\s+)?expired|invalid\s+(?:link|url)", re.I)
ACCOUNT_EXISTS_TEXT = re.compile(
    r"(?:account|user|e-?mail)\s+(?:already\s+exists|is\s+already\s+(?:registered|in\s+use|taken))|"
    r"already\s+(?:have|has)\s+an\s+account|already\s+registered", re.I)

NON_APPLICATION_FIELD_RX = re.compile(
    r"^\s*search|search\s+(?:jobs|for|by|keywords?|here)|keywords?|job\s+title,|what\s+job|^where\b|"
    r"newsletter|subscribe|job\s+alerts?|filter|sort\s+by|^go\s+to\b|captcha|recaptcha|not\s+a\s+robot|^q$|^query$|"
    r"honey\s*pot|\bhp[_-]?(?:field|name|email)\b|leave\s+(?:this\s+)?(?:field\s+)?(?:blank|empty)|do\s+not\s+(?:fill|enter|change)|"
    r"\bbot[_-]?(?:field|check|trap)\b|anti[_-]?spam|"
    r"enter\s+(?:your\s+)?e-?mail\s+(?:to|for)\s+(?:subscribe|updates|alerts)|language|currency\s+selector",
    re.I)
PROMO_DIALOG_RX = re.compile(
    r"talent\s+(?:community|network|pool|club)|newsletter|subscribe|job\s+alerts?|stay\s+(?:in\s+touch|connected|updated)|"
    r"get\s+notified|join\s+our\s+(?:community|network|mailing)|sign\s+up\s+for\s+(?:updates|alerts|our)|"
    r"\bsurvey\b|rate\s+your\s+experience|chat\s+with\s+us|download\s+(?:our|the)\s+app|turn\s+on\s+notifications",
    re.I)
CONFIRM_PASSWORD_RX = re.compile(r"confirm|re-?enter|repeat|retype|verify|again", re.I)


def norm(text: str) -> str:
    t = re.sub(r"\s+", " ", (text or "").replace(" ", " ")).strip()
    return t.strip(" .:*")


def classify_element(text: str, *, tag: str = "", type_: str = "", href: str = "", aria: str = "",
                     form_context: bool = False, cookie_context: bool = False) -> Kind:
    """Classify a clickable element by its visible text (or aria-label / value)."""
    t = norm(text or aria)
    low = t.lower()
    if not t:
        if re.search(r"close|dismiss", aria or "", re.I):
            return Kind.CLOSE
        return Kind.OTHER
    if len(t) > 90:  # cards / paragraphs that happen to be clickable
        return Kind.OTHER
    if SSO_RX.search(t):
        return Kind.SSO
    if form_context and FORM_SAVE_RX.search(t):
        return Kind.SUBMIT  # "Save" on an application form/dialog sends it: gated like any submit
    if SEARCH_BUTTON_RX.search(t):
        return Kind.OTHER
    if APPLY_AVOID_RX.search(t):
        return Kind.DANGER if re.search(r"linked\s*in|indeed|seek|google|facebook|xing|glassdoor", low) else Kind.OTHER
    if DANGER_RX.search(t) or CONTACT_BUTTON_RX.search(t):
        return Kind.DANGER
    if href and re.match(r"^\s*(?:mailto:|tel:|javascript:void\(0\);?\s*logout)", href, re.I):
        return Kind.DANGER
    if cookie_context and COOKIES_RX.search(t):
        return Kind.COOKIES
    if GUEST_RX.search(t):
        return Kind.GUEST
    if CLOSE_RX.search(t) or re.search(r"\bclose\b|\bdismiss\b", aria or "", re.I) and len(t) <= 2:
        return Kind.CLOSE
    if BACK_RX.search(t):
        return Kind.BACK
    if UPLOAD_RX.search(t):
        return Kind.UPLOAD
    if form_context:
        if NEXT_RX.search(t):
            return Kind.NEXT
        if SUBMIT_RX.search(t):
            return Kind.SUBMIT
        if APPLY_RX.search(t):  # e.g. "Apply for this job" as the form's own button
            return Kind.SUBMIT
    else:
        if APPLY_RX.search(t):
            return Kind.APPLY
        if NEXT_RX.search(t):
            return Kind.NEXT
        if SUBMIT_RX.search(t):
            return Kind.SUBMIT
    if SIGNUP_RX.search(t):
        return Kind.SIGNUP
    if SIGNIN_RX.search(t):
        return Kind.SIGNIN
    if type_ == "submit" and form_context:
        return Kind.SUBMIT  # unknown text on a submit button inside a form: treat as irreversible
    return Kind.OTHER


def success_evidence(text: str) -> str:
    m = SUCCESS_TEXT.search(text or "")
    return m.group(0) if m else ""


def already_applied_evidence(text: str) -> str:
    m = ALREADY_APPLIED_TEXT.search(text or "")
    return m.group(0) if m else ""


def expired_evidence(text: str) -> str:
    m = EXPIRED_TEXT.search(text or "")
    return m.group(0) if m else ""


def is_application_field(label: str, name: str = "", placeholder: str = "") -> bool:
    blob = " ".join(x for x in (label, name, placeholder) if x)
    return not NON_APPLICATION_FIELD_RX.search(blob)
