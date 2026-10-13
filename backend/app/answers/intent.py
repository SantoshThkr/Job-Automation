"""Question intent: what an application question is really asking, independent of its exact wording.

"Why are you interested in this job?", "What attracts you to this role?" and "Why are you applying for this
position?" are all WHY_ROLE. The category decides how the answer is produced (profile fact, derivation,
grounded generation, or asking you). Classification is local and deterministic (cue patterns + fuzzy
similarity to paraphrase prototypes); the local LLM is only consulted when that signal is weak.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import StrEnum

from rapidfuzz import fuzz

log = logging.getLogger(__name__)


class Category(StrEnum):
    PROFILE_FACT = "PROFILE_FACT"
    DERIVED_FACT = "DERIVED_FACT"
    EXPERIENCE = "EXPERIENCE"
    TECHNICAL_EXPERIENCE = "TECHNICAL_EXPERIENCE"
    CURRENT_RESPONSIBILITIES = "CURRENT_RESPONSIBILITIES"
    PROJECT_EXPERIENCE = "PROJECT_EXPERIENCE"
    WHY_ROLE = "WHY_ROLE"
    WHY_COMPANY = "WHY_COMPANY"
    WHY_HIRE = "WHY_HIRE"
    STRENGTHS = "STRENGTHS"
    ACHIEVEMENT = "ACHIEVEMENT"
    CAREER_GOALS = "CAREER_GOALS"
    ABOUT = "ABOUT"
    COVER_LETTER = "COVER_LETTER"
    PREFERENCE = "PREFERENCE"
    SALARY = "SALARY"
    NOTICE_PERIOD = "NOTICE_PERIOD"
    LOCATION = "LOCATION"
    EDUCATION = "EDUCATION"
    DEMOGRAPHIC = "DEMOGRAPHIC"
    LEGAL_DECLARATION = "LEGAL_DECLARATION"
    SENSITIVE_ID = "SENSITIVE_ID"  # passport / PAN / Aadhaar / SSN numbers, date of birth...
    VISA = "VISA"
    CUSTOM = "CUSTOM"
    NOT_A_QUESTION = "NOT_A_QUESTION"  # chatbot greetings / thank-you / instructions
    UNKNOWN = "UNKNOWN"


# Written answers built from your verified information (never facts invented about you).
GENERATIVE = {Category.WHY_ROLE, Category.WHY_COMPANY, Category.WHY_HIRE, Category.STRENGTHS,
              Category.TECHNICAL_EXPERIENCE, Category.CURRENT_RESPONSIBILITIES, Category.PROJECT_EXPERIENCE,
              Category.ACHIEVEMENT, Category.CAREER_GOALS, Category.ABOUT, Category.COVER_LETTER}
# Answers that depend on the specific employer/job: a saved answer is not reused for other companies.
CONTEXT_SPECIFIC = {Category.WHY_ROLE, Category.WHY_COMPANY, Category.COVER_LETTER, Category.WHY_HIRE}
# Only you can answer these when the profile has nothing (never derived or generated).
ASK_IF_MISSING = {Category.SENSITIVE_ID, Category.VISA, Category.LEGAL_DECLARATION, Category.DEMOGRAPHIC}


@dataclass
class Intent:
    category: Category
    confidence: float
    skill_focus: list[str]
    narrative: bool  # a written answer is expected (vs a number / choice / short fact)
    evidence: str = ""


def _rx(*parts: str) -> re.Pattern[str]:
    return re.compile("|".join(parts), re.I)


# (category, strong cue pattern). Order matters only for equal scores.
_CUES: list[tuple[Category, re.Pattern[str]]] = [
    (Category.NOT_A_QUESTION, _rx(
        r"^(?:hi|hello|dear)\b[^?]{0,140}(?:thank|kindly|please\s+answer|interest)",
        r"^thank(?:s|\s+you)\b[^?]*$", r"^(?:kindly|please)\s+answer\s+(?:all|the|these|following)",
        r"^(?:great|awesome|got\s+it|noted|okay|ok)[!.,\s]*$", r"we\s+will\s+(?:get\s+back|contact|reach\s+out)[^?]*$",
        r"^your\s+(?:responses?|answers?)\s+(?:have|has)\s+been\s+(?:recorded|saved|submitted)")),
    (Category.COVER_LETTER, _rx(r"cover\s*letter", r"covering\s+letter", r"letter\s+of\s+(?:motivation|interest)",
                                # a bare "Message" box on an application form is a cover note (contact forms are
                                # never filled - see navigator/observe.py)
                                r"^(?:your\s+)?(?:message|cover\s+note|note\s+to\s+(?:the\s+)?(?:recruiter|hiring\s+team))$")),
    (Category.WHY_COMPANY, _rx(
        r"why\s+(?:do\s+you\s+want\s+to\s+|would\s+you\s+like\s+to\s+)?(?:work|join)\s+(?:at|for|with)\s+(?!this\s+(?:role|position|job))",
        r"why\s+(?:our|this)\s+(?:company|organi[sz]ation|firm|team)", r"what\s+do\s+you\s+know\s+about\s+(?:us|our|the\s+company)",
        r"why\s+(?:join\s+)?us\b", r"what\s+(?:excites|attracts|interests)\s+you\s+about\s+(?:us|our\s+company|working\s+(?:at|here))",
        # "Why Acme Labs?": a name after "why", not a sentence
        r"^why\s+(?!(?:do|does|did|are|is|was|should|would|will|can|could|have|has|this|that|the|you|we|i|not|an?|it|so|"
        r"join|work|choose|apply|leave|change|switch|now|us|our)\b)[a-z0-9][\w.&' -]{1,40}\??$",
        r"why\s+do\s+you\s+want\s+to\s+(?:work|join)\s+(?:here|us)")),
    (Category.WHY_ROLE, _rx(
        r"why\s+(?:are\s+you|you\s+are)\s+(?:interested|applying|keen)", r"why\s+(?:do|did)\s+you\s+(?:want|wish|apply)",
        r"what\s+(?:attracts|excites|interests|draws|motivates)\s+you", r"why\s+this\s+(?:role|position|job|opportunity)",
        r"interest(?:ed)?\s+in\s+(?:this|the)\s+(?:role|position|job|opportunity)", r"motivat\w*\s+(?:for|to)\s+(?:apply|join)",
        r"reason\s+for\s+(?:applying|your\s+application|interest)", r"why\s+(?:are\s+you\s+)?(?:looking\s+for\s+a\s+change|considering\s+a\s+move)")),
    (Category.WHY_HIRE, _rx(
        r"why\s+should\s+(?:we|i|they)\s+(?:hire|select|choose|consider)", r"(?:good|great|strong|right|best)\s+(?:fit|candidate|match)",
        r"what\s+makes\s+you\s+(?:a\s+|the\s+)?(?:good|great|ideal|right|suitable|strong|best)",
        r"what\s+(?:value|can\s+you\s+bring|will\s+you\s+bring|do\s+you\s+bring)", r"why\s+are\s+you\s+(?:suitable|qualified)",
        r"how\s+(?:are\s+you|would\s+you\s+be)\s+(?:a\s+)?(?:fit|suited|qualified)", r"what\s+sets\s+you\s+apart")),
    (Category.STRENGTHS, _rx(
        r"(?:key|top|core|greatest|biggest|main)?\s*strengths?\b", r"what\s+are\s+you\s+(?:good|best)\s+at",
        r"core\s+competenc", r"(?:top|key|core|primary)\s+skills", r"area\s+of\s+expertise", r"technical\s+expertise")),
    (Category.ACHIEVEMENT, _rx(r"achievement", r"accomplishment", r"proud(?:est)?\s+of", r"biggest\s+(?:win|success|impact)",
                               r"(?:award|recognition)s?\s+(?:received|won)")),
    (Category.PROJECT_EXPERIENCE, _rx(
        r"(?:challenging|difficult|complex|recent|favourite|favorite|significant|major|interesting)\s+project",
        r"(?:describe|tell\s+(?:us|me)\s+about|walk\s+us\s+through)\s+(?:a|one|your)\s+(?:\w+\s+)?project",
        r"project\s+(?:you\s+(?:worked|are\s+working)|that\s+you)", r"projects?\s+(?:you\s+have|you've)\s+(?:done|built|worked)")),
    (Category.CURRENT_RESPONSIBILITIES, _rx(
        r"(?:current|present|day[\s-]to[\s-]day|daily|key)\s+(?:roles?\s+and\s+)?responsibilit", r"roles?\s+(?:and|&)\s+responsibilit",
        r"what\s+(?:do\s+you\s+do|are\s+you\s+working\s+on)\s+(?:in|at)\s+your\s+(?:current|present)",
        r"describe\s+your\s+(?:current|present)\s+(?:role|job|position|work)", r"(?:current|present)\s+(?:role|job)\s+(?:description|summary)")),
    (Category.CAREER_GOALS, _rx(r"career\s+(?:goals?|aspirations?|plans?|objectives?)", r"where\s+do\s+you\s+see\s+yourself",
                                r"(?:long|short)[\s-]term\s+(?:goals?|plans?)", r"professional\s+goals?")),
    (Category.ABOUT, _rx(r"tell\s+(?:us|me)\s+(?:a\s+(?:little|bit)\s+)?about\s+yourself", r"introduce\s+yourself",
                         r"describe\s+yourself", r"(?:professional|profile|career)\s+summary", r"^summary$", r"^about\s+(?:you|yourself|me)$",
                         r"brief\s+(?:introduction|summary|bio)")),
    (Category.SENSITIVE_ID, _rx(r"passport\s*(?:number|no\.?|#)", r"\bpan\s*(?:card)?\s*(?:number|no)", r"aadha?ar",
                                r"\bssn\b|social\s+security", r"date\s+of\s+birth|\bdob\b|birth\s*date", r"driving\s+licen[cs]e\s*(?:number|no)",
                                r"bank\s+account", r"\buan\b|universal\s+account\s+number", r"father'?s\s+name|mother'?s\s+name",
                                # personal facts the profile has no field for: asked once, never derived or guessed
                                r"^(?:your\s+)?age(?:\s*\(?in\s+years\)?)?$|what\s+is\s+your\s+age|how\s+old\s+are\s+you",
                                r"home\s*town|native\s+(?:place|city|town|state)", r"blood\s+group", r"marital\s+status",
                                r"emergency\s+contact", r"spouse'?s?\s+name",
                                # a one-time code is typed by you (see browser.engine.OTP_TEXT)
                                r"verification\s+code|one[\s-]time\s+(?:password|code|pass\s*code)|\botp\b|security\s+code")),
    (Category.VISA, _rx(r"\bvisa\b", r"work\s+permit", r"green\s*card", r"\bh-?1b\b", r"citizenship|nationality|\bcitizen\b",
                        r"work\s+authori[sz]ation", r"authori[sz]ed\s+to\s+work", r"right\s+to\s+work", r"eligible\s+to\s+work",
                        r"legally\s+(?:authori[sz]ed|eligible|permitted)", r"residency\s+status|immigration\s+status")),
    (Category.LEGAL_DECLARATION, _rx(
        r"convicted|criminal|felony|arrest", r"background\s+(?:check|verification)", r"non[\s-]?compete|bond\s+agreement",
        r"(?:i\s+)?(?:hereby\s+)?(?:declare|certify|confirm|acknowledge|consent|agree)\b", r"terms\s+(?:and|&)\s+conditions|privacy\s+policy",
        r"drug\s+(?:test|screen)", r"legal\s+(?:proceedings|action)")),
    (Category.DEMOGRAPHIC, _rx(r"\bgender\b|\bsex\b", r"pronoun", r"veteran|military", r"disabilit", r"ethnic|\brace\b",
                               r"marital\s+status", r"religion|caste|category\s*\((?:sc|st|obc)", r"sexual\s+orientation")),
    (Category.SALARY, _rx(r"\bctc\b|salary|compensation|package|remuneration|\bpay\b|\blpa\b|stipend|hike")),
    (Category.NOTICE_PERIOD, _rx(r"notice\s+period", r"how\s+soon", r"(?:earliest|possible)\s+(?:start|joining)", r"when\s+can\s+you\s+(?:start|join)",
                                 r"join(?:ing)?\s+(?:date|immediately|time)", r"last\s+working\s+day|\blwd\b|relieving\s+date",
                                 r"available\s+to\s+(?:start|join)", r"serving\s+notice", r"date\s+of\s+(?:joining|leaving|relieving)",
                                 r"lead\s+time\s+(?:to|for)\s+join", r"days\s+(?:can|will|would|do)?\s*you\s+(?:can\s+|need\s+to\s+)?join")),
    (Category.LOCATION, _rx(r"relocat", r"\bcity\b", r"location", r"where\s+are\s+you\s+(?:currently\s+)?(?:based|located|living)",
                            r"residing|reside\b", r"commut", r"willing\s+to\s+(?:move|shift)", r"based\s+out\s+of", r"pin\s*code|postal|zip")),
    (Category.PREFERENCE, _rx(r"willing\s+to", r"comfortable\s+(?:with|working)", r"open\s+to", r"prefer(?:red|ence)?\b",
                              r"shifts?\b", r"travel", r"remote|hybrid|on[\s-]?site|work\s+from\s+office", r"availab(?:le|ility)\s+for",
                              r"interview\s+availab", r"\bavailability\b")),
    (Category.EDUCATION, _rx(r"degree|qualification|education|graduat|university|college|institut|\bcgpa\b|\bgpa\b|percentage|marks|"
                             r"field\s+of\s+study|specialization|\bmca\b|\bbca\b|b\.?\s?tech|m\.?\s?tech|bachelor|master")),
    (Category.EXPERIENCE, _rx(r"years?\s+of\s+(?:[\w.+#/-]+\s+){0,3}experience", r"how\s+many\s+(?:years|months)", r"total\s+(?:\w+\s+)?experience",
                              r"(?:overall|relevant|work|professional|industry)\s+experience", r"\byoe\b", r"experience\s+(?:level|in\s+years)",
                              r"how\s+long\s+have\s+you")),
    (Category.TECHNICAL_EXPERIENCE, _rx(
        r"(?:describe|explain|tell\s+(?:us|me)\s+about|share|elaborate\s+on)\s+(?:your\s+)?(?:[\w.+#/-]+\s+){0,3}(?:experience|expertise|background|knowledge|exposure|work)",
        r"how\s+(?:experienced|proficient|familiar|comfortable|skilled)\s+are\s+you", r"(?:\w+\s+)?(?:background|expertise)\s+(?:in|with)",
        r"level\s+of\s+(?:expertise|proficiency)\s+(?:in|with)", r"hands[\s-]on\s+experience\s+(?:in|with)",
        r"what\s+(?:is|has\s+been)\s+your\s+experience\s+(?:with|in|of)")),
    (Category.DERIVED_FACT, _rx(r"(?:previously|ever|before)\s+(?:worked|been\s+employed|employed)",
                                r"worked\s+(?:for|at|with)\s+(?:us|this\s+company|[A-Z]?\w+)\s+(?:before|previously|earlier)",
                                r"have\s+you\s+(?:ever\s+)?worked\s+(?:for|at|with)\b", r"former\s+employee",
                                r"(?:at\s+least|over|above)\s+18|legal\s+age", r"currently\s+(?:employed|working)",
                                r"direct\s+payroll|on\s+(?:the\s+)?payroll|third[\s-]party\s+payroll",
                                r"(?:position|role|job(?:\s+title)?)\s+(?:you\s+are\s+|you're\s+)?(?:applying|applied)\s+(?:for|to)",
                                r"(?:position|role|job)\s+applied\s+for")),
    (Category.PROFILE_FACT, _rx(r"\bname\b", r"e-?mail", r"phone|mobile|contact\s+(?:number|no)", r"linked\s*in|git\s*hub|portfolio|website",
                                r"current\s+(?:company|employer|organi[sz]ation)",
                                r"(?:current|present|most\s+recent)\s+(?:job\s+)?(?:designation|title|role|position)(?!\s+(?:and|&|description|summary))",
                                r"^(?:job\s+)?title$|\bjob\s+title\b|^designation$|^your\s+(?:job\s+)?title$", r"address")),
]

# Paraphrase prototypes: catch wordings no cue pattern anticipated (fuzzy token similarity).
_PROTOTYPES: dict[Category, list[str]] = {
    Category.WHY_ROLE: ["why are you interested in this role", "why do you want this job", "what attracts you to this position",
                        "why are you applying for this role", "why do you want to join this position",
                        "what interests you about this opportunity", "your motivation for applying"],
    Category.WHY_COMPANY: ["why do you want to work for our company", "why do you want to join us", "what do you know about our company"],
    Category.WHY_HIRE: ["why should we hire you", "why are you a good fit for this role", "what makes you the right candidate",
                        "what value can you bring to the team"],
    Category.STRENGTHS: ["what are your key strengths", "what are your strengths", "what are you best at", "your core skills"],
    Category.TECHNICAL_EXPERIENCE: ["describe your experience with react", "tell us about your react experience",
                                    "how experienced are you with react", "describe your react background",
                                    "what is your experience with typescript"],
    Category.CURRENT_RESPONSIBILITIES: ["what are your current responsibilities", "describe your current role",
                                        "what do you do in your current job"],
    Category.PROJECT_EXPERIENCE: ["tell us about a challenging project you worked on", "describe a project you are proud of"],
    Category.ACHIEVEMENT: ["what is your biggest professional achievement", "describe an accomplishment"],
    Category.CAREER_GOALS: ["what are your career goals", "where do you see yourself in five years"],
    Category.ABOUT: ["tell us about yourself", "introduce yourself briefly", "professional summary"],
}

_YEARS_SIGNAL = re.compile(r"\byears?\b|\byrs?\b|\bmonths?\b|how\s+many|how\s+long|number\s+of|\bin\s+numbers\b|\btotal\b", re.I)
_NARRATIVE_SIGNAL = re.compile(r"describe|explain|tell\s+(?:us|me)|elaborate|share|why|what\s+(?:attracts|makes|are\s+your)|"
                               r"how\s+(?:experienced|proficient|would\s+you)|summar", re.I)


def question_skills(question: str) -> list[str]:
    """Technologies a question asks about (aliases: 'Hooks' -> React; Next.js is not JavaScript)."""
    from app.matching.skills import extract_skills, skill_def

    found = [s for s in extract_skills(question) if (d := skill_def(s)) and not d.signal_only]
    if re.search(r"\bhooks?\b", question, re.I) and "React" not in found:
        found.append("React")  # "React hooks" / "Hooks" questions in React jobs
    if "JavaScript" in found and not re.search(r"javascript|java\s+script|\bes6|\bjs\b(?<!\.js)", question, re.I):
        found.remove("JavaScript")  # only matched the ".js" of Next.js / Node.js / React.js
    return found


def classify(question: str, *, field_type: str = "text", options: list[str] | None = None) -> Intent:
    """Deterministic intent classification (no network)."""
    from app.answers.question_bank import repair_words

    q = re.sub(r"\s+", " ", (question or "").replace("*", " ")).strip()
    low = repair_words(q)  # 'Notice - Period' / 'D.O.B' / 'mobilephone' read as the ordinary labels they are
    skills = question_skills(q)
    textual = field_type in {"textarea", "contenteditable"}
    numeric = field_type == "number" or bool(options and any(re.search(r"\d", o) for o in options))
    narrative = textual or (bool(_NARRATIVE_SIGNAL.search(low)) and not numeric and not options)
    if not low:
        return Intent(Category.UNKNOWN, 0.0, skills, narrative)

    scores: dict[Category, tuple[float, str]] = {}
    for cat, rx in _CUES:
        m = rx.search(low)
        if m:
            scores.setdefault(cat, (0.9, m.group(0)))
    for cat, protos in _PROTOTYPES.items():
        best = max((fuzz.token_set_ratio(low, p) for p in protos), default=0) / 100
        if best >= 0.72 and best * 0.95 > scores.get(cat, (0.0, ""))[0]:
            scores[cat] = (round(best * 0.95, 3), "similar wording")

    if not scores:
        cat = Category.TECHNICAL_EXPERIENCE if (skills and narrative) else Category.UNKNOWN
        return Intent(cat, 0.6 if cat != Category.UNKNOWN else 0.2, skills, narrative)

    # experience: numbers -> EXPERIENCE (derived), prose about a skill -> TECHNICAL_EXPERIENCE (generated)
    if Category.EXPERIENCE in scores and Category.TECHNICAL_EXPERIENCE in scores:
        if _YEARS_SIGNAL.search(low) and not textual:
            scores.pop(Category.TECHNICAL_EXPERIENCE)
        else:
            scores.pop(Category.EXPERIENCE)
    if Category.TECHNICAL_EXPERIENCE in scores and not skills and not narrative:
        scores.pop(Category.TECHNICAL_EXPERIENCE)
    # specific beats generic: an interview-availability or relocation question is not about "availability" alone
    priority = [Category.NOT_A_QUESTION, Category.SENSITIVE_ID, Category.VISA, Category.COVER_LETTER, Category.WHY_COMPANY,
                Category.WHY_ROLE, Category.WHY_HIRE, Category.ACHIEVEMENT, Category.PROJECT_EXPERIENCE,
                Category.CURRENT_RESPONSIBILITIES, Category.CAREER_GOALS, Category.ABOUT, Category.STRENGTHS,
                Category.SALARY, Category.NOTICE_PERIOD, Category.DEMOGRAPHIC, Category.LEGAL_DECLARATION,
                Category.EXPERIENCE, Category.TECHNICAL_EXPERIENCE, Category.EDUCATION, Category.LOCATION,
                Category.PREFERENCE, Category.PROFILE_FACT]
    if "?" in q and Category.NOT_A_QUESTION in scores and len(scores) > 1:
        scores.pop(Category.NOT_A_QUESTION)  # "Thank you! What is your notice period?"
    if not scores:  # every cue was ruled out above (e.g. a bare "Experience" label)
        cat = Category.EXPERIENCE if re.fullmatch(r"(?:total\s+|work\s+)?experience", low) else Category.UNKNOWN
        return Intent(cat, 0.6 if cat != Category.UNKNOWN else 0.2, skills, narrative)
    best_cat = max(scores, key=lambda c: (scores[c][0], -priority.index(c) if c in priority else -99))
    conf, evidence = scores[best_cat]
    if best_cat == Category.STRENGTHS and not narrative and (options or field_type in {"radio", "select", "checkbox"}):
        best_cat = Category.CUSTOM  # "Rate your strength in X" style choice questions
    return Intent(best_cat, conf, skills, narrative or best_cat in GENERATIVE, evidence)


_LLM_SCHEMA = {
    "type": "object",
    "properties": {"category": {"type": "string", "enum": [c.value for c in Category]},
                   "confidence": {"type": "number"}},
    "required": ["category", "confidence"],
}


async def classify_with_llm(provider, question: str, field_type: str, options: list[str]) -> Intent | None:
    """Fallback for wordings the deterministic classifier does not recognise."""
    from app.ai.provider import AIResponseError, AIUnavailable

    prompt = (f"Job application question ({field_type}): {question}\n"
              + (f"Options: {', '.join(options[:15])}\n" if options else "")
              + "Classify what it asks. Categories: " + ", ".join(c.value for c in Category if c != Category.UNKNOWN)
              + ". WHY_ROLE=why this job; WHY_HIRE=why you/fit; TECHNICAL_EXPERIENCE=describe experience with a "
                "technology; EXPERIENCE=years of experience; PREFERENCE=willingness/availability; CUSTOM=a specific "
                "fact about the person not covered above. Return JSON {category, confidence}.")
    try:
        data = await provider.chat_json("You classify job application questions. JSON only.", prompt,
                                        json_schema=_LLM_SCHEMA, max_tokens=60, temperature=0.0)
        cat = Category(str(data.get("category")))
        conf = float(data.get("confidence") or 0)
    except (AIUnavailable, AIResponseError, ValueError, TypeError, AttributeError) as exc:
        log.debug("LLM intent classification failed: %s", exc)
        return None
    skills = question_skills(question)
    return Intent(cat, min(0.8, conf / 100 if conf > 1 else conf), skills,
                  cat in GENERATIVE or field_type in {"textarea", "contenteditable"}, "local AI")
