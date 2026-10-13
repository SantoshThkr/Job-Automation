"""The answer engine: one place that answers every application question, on every portal and site.

    understand the question (intent)
      -> your own fixed answers + profile facts            (known)
      -> derivations from verified facts                   (derived: dates, skill years, locations...)
      -> your previously verified answers                  (memory; the most specific scope that applies wins:
                                                             this job > this employer > this portal > general)
      -> grounded generation for written answers           (generated: local AI, validated; composer fallback)
      -> local-AI derivation for unrecognised facts        (only from the profile, guarded)
      -> ask you                                           (truly unknown: visas, ID numbers, achievements...)

Every answer carries its category, mode, grounding sources and confidence. Nothing about you is invented:
generated text may only combine and rephrase verified facts and the job description.
"""

from __future__ import annotations

import inspect
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

from app.ai.guard import check_text
from app.ai.job_assistant import AIResponseError, AIUnavailable, JobAssistant, generate_grounded
from app.answers.compose import Budget, Composer
from app.answers.grounding import attribute_sources, check_claims
from app.answers.intent import ASK_IF_MISSING, CONTEXT_SPECIFIC, GENERATIVE, Category, Intent, classify, \
    classify_with_llm
from app.answers.knowledge import CandidateKnowledge
from app.answers.options import NO_WORDS, YES_WORDS, choose_option, real_options
from app.answers.question_bank import OPEN_ENDED, Answer, QuestionContext, normalize_question, resolve
from app.config import get_settings
from app.profile import Profile

log = logging.getLogger(__name__)


@dataclass
class MemoryHit:
    answer: str
    question: str = ""
    scope: str = "global"  # global | category | company | portal | job
    company: str = ""
    portal: str = ""
    job_key: str = ""


# (normalised question[, company, portal, job title]) -> your saved answer(s) for it
MemoryLookup = Callable[..., "str | MemoryHit | list[MemoryHit] | None"]

# rule handler -> profile keys it reads (for "Answer source")
_HANDLER_SOURCES = {
    "h_city": ["personal.current_city"], "h_full_name": ["personal.first_name", "personal.last_name"],
    "h_phone": ["personal.phone"], "h_country_code": ["personal.phone_country_code"],
    "h_salary_current": ["professional.current_ctc_lpa"], "h_salary_expected": ["professional.expected_ctc_lpa"],
    "h_notice": ["professional.notice_period_days", "professional.serving_notice", "professional.last_working_day"],
    "h_last_working_day": ["professional.last_working_day"],
    "h_experience": ["professional.total_experience_years", "skills.years", "professional.work_history"],
    "h_relocate": ["work_preferences.willing_to_relocate", "work_preferences.preferred_locations"],
    "h_location_choice": ["personal.current_city", "work_preferences.preferred_locations"],
    "h_preferred_location": ["work_preferences.preferred_locations"], "h_onsite": ["work_preferences"],
    "h_sponsorship": ["work_preferences.requires_sponsorship"],
    "h_authorization": ["work_preferences.authorized_countries"], "h_visa": ["work_preferences.authorized_countries"],
    "h_citizenship": ["work_preferences.citizenship"],
    "h_current_company": ["professional.current_company"], "h_current_title": ["professional.current_title"],
    "h_employed": ["professional.current_company", "professional.last_working_day"],
    "h_degree": ["professional.education"], "h_institution": ["professional.education"],
    "h_grad_year": ["professional.education"], "h_grade": ["professional.education"],
    "h_field_of_study": ["professional.education"], "h_previous_employment": ["professional.work_history"],
    "h_skill_yes_no": ["skills.primary", "skills.secondary"], "h_about": ["professional.summary"],
    "h_interview_availability": ["work_preferences.interview_availability"],
    "h_experience_level": ["professional.total_experience_years"], "h_adult": ["professional.total_experience_years"],
}
_PERSONAL = re.compile(r"^h_(first|last|email|linkedin|github|postal|state|country|address)$")

MODE_LABEL = {
    "profile": "profile fact", "derived": "derived from your profile", "ai_generated": "AI generated (grounded)",
    "composed": "written from your profile", "memory": "your saved answer", "ai_derived": "AI derived from profile",
    "custom": "your custom answer", "user": "answered by you", "ask": "manual input required",
    "skipped": "left blank (optional)", "not_a_question": "not a question",
}


def describe_sources(sources: list[str]) -> str:
    """'profile + job description' style summary for logs."""
    parts = []
    if any(not s.startswith("job.") and s not in {"page", "your saved answer"} for s in sources):
        parts.append("profile")
    if any(s.startswith("job.") for s in sources):
        parts.append("job description")
    if "page" in sources:
        parts.append("application page")
    if "your saved answer" in sources:
        parts.append("your saved answer")
    return " + ".join(parts) or "-"


class Answerer:
    def __init__(self, profile: Profile, assistant: JobAssistant | None = None,
                 memory_lookup: MemoryLookup | None = None, min_confidence: float | None = None,
                 allow_ai: bool = True) -> None:
        self.profile = profile
        self.assistant = assistant
        self.memory_lookup = memory_lookup or (lambda _q: None)
        try:  # a lookup that takes the job chooses the most specific saved answer itself (engine.recorder)
            self._lookup_takes_job = len(inspect.signature(self.memory_lookup).parameters) > 1
        except (TypeError, ValueError):
            self._lookup_takes_job = False
        self.min_confidence = get_settings().ai_min_confidence if min_confidence is None else min_confidence
        self.allow_ai = allow_ai
        self.knowledge = CandidateKnowledge(profile)
        self.composer = Composer(self.knowledge)
        self._ai_ok: bool | None = None

    async def _ai_available(self) -> bool:
        if not (self.allow_ai and self.assistant is not None):
            return False
        if self._ai_ok is None or self._ai_ok:
            self._ai_ok = await self.assistant.available()
        return self._ai_ok

    async def answer(self, ctx: QuestionContext) -> Answer:
        try:
            ans = await self._answer(ctx)
        except Exception as exc:  # never let one question crash an application
            log.exception("Answering failed for %r", ctx.question)
            ans = Answer(None, 0.0, "error", needs_review=True, note=f"error: {exc}", mode="ask")
        return self._finalize(ctx, ans)

    # ------------------------------------------------------------------ pipeline
    async def intent(self, ctx: QuestionContext, *, rule_matched: bool = False) -> Intent:
        intent = classify(ctx.question, field_type=ctx.field_type, options=real_options(ctx.options))
        if intent.category == Category.UNKNOWN and ctx.required and not rule_matched and await self._ai_available():
            via_ai = await classify_with_llm(self.assistant.provider, ctx.question, ctx.field_type,
                                             real_options(ctx.options))
            # a required form field always needs a value: only the deterministic cues may call text "not a question"
            if via_ai is not None and via_ai.confidence >= 0.5 and via_ai.category != Category.NOT_A_QUESTION:
                return via_ai
        return intent

    async def _answer(self, ctx: QuestionContext) -> Answer:
        nq = normalize_question(ctx.question)
        det = resolve(ctx, self.profile)  # profile rules first: a recognised fact never depends on the local AI's guess
        intent = await self.intent(ctx, rule_matched=det is not None)
        cat = intent.category

        def tag(a: Answer, mode: str, sources: list[str] | None = None) -> Answer:
            a.category, a.mode = cat.value, mode
            if sources is not None:
                a.sources = sources
            return a

        if cat == Category.NOT_A_QUESTION:
            return tag(Answer("", 0.95, "derived", note="a message, not a question"), "not_a_question", [])

        # 1. your own fixed answers (profile.custom_answers)
        for ca in self.profile.custom_answers:
            pat = ca.pattern.strip()
            if not pat:
                continue
            hit = re.search(pat[3:], nq, re.I) if pat.startswith("re:") else pat.lower() in nq
            if hit:
                opts = real_options(ctx.options)
                if opts and ctx.field_type in {"select", "radio", "checkbox_group"} and choose_option(ca.answer, opts) is None:
                    continue  # your fixed wording is not among this form's choices: the profile rules choose one
                return tag(Answer(ca.answer, 0.95, "custom", note=f"custom answer '{pat}'"), "custom",
                           ["custom_answers"])

        # 2. profile facts and derivations
        missing: Answer | None = None
        wants_prose = (ctx.field_type in {"textarea", "contenteditable"} and cat in GENERATIVE | {Category.EXPERIENCE}
                       and intent.narrative)
        if det is not None and det is not OPEN_ENDED and not det.open_ended:
            short_fact_for_prose = wants_prose and (det.numeric is not None or (
                cat in GENERATIVE and len(str(det.value or "").split()) < 8))  # a title is not a written answer
            if det.value is not None and not short_fact_for_prose:
                handler = det.rule or (det.note or "").replace("rule: ", "")
                sources = det.sources or _HANDLER_SOURCES.get(handler) or (
                    ["personal." + _PERSONAL.match(handler).group(1)] if _PERSONAL.match(handler) else [])
                mode = "profile" if det.source == "profile" else "derived"
                answer = tag(det, mode, sources or ["profile"])
                if not det.needs_review:
                    return answer
                remembered = self._memory(nq, ctx, cat) or self._keyed_memory(ctx, cat)  # confirmed by you once
                if remembered is not None:
                    return tag(remembered, "memory", ["your saved answer"])
                return answer
            if det.value is None:
                missing = det  # recognised, but the fact is not in your profile

        # 3. answers you gave before (the most specific that applies: this job, employer, portal, then general).
        #    Your own words always come before anything written for you.
        remembered = self._memory(nq, ctx, cat) or self._keyed_memory(ctx, cat)
        if remembered is not None:
            return tag(remembered, "memory", ["your saved answer"])

        # 4. written answers grounded in your verified information
        generative = cat in GENERATIVE or det is OPEN_ENDED or (det is not None and det.open_ended) or wants_prose
        if generative and missing is None:
            optional_ok = cat in {Category.WHY_ROLE, Category.WHY_COMPANY, Category.WHY_HIRE, Category.COVER_LETTER,
                                  Category.ABOUT}
            if ctx.required or optional_ok:
                gen = await self._generate(ctx, intent)
                if gen is not None:
                    return tag(gen, gen.mode)

        # 5. never guess optional questions
        if not ctx.required:
            return tag(Answer("", 0.9, "derived", note="optional question with no verified answer - left blank"),
                       "skipped", [])

        # 6. the local AI may derive an unrecognised fact from your profile (never for IDs, visas, legal, EEO)
        if missing is None and cat not in ASK_IF_MISSING | GENERATIVE and await self._ai_available():
            try:
                res = await self.assistant.answer_factual(self.profile, ctx.question, ctx.field_type,
                                                          real_options(ctx.options), ctx.job)
            except AIUnavailable as exc:
                res = None
                log.info("AI unavailable: %s", exc)
            if res is not None and res.answer:
                used = [u for u in (res.raw.get("profile_fields_used") or []) if isinstance(u, str)]
                return tag(Answer(res.answer, res.confidence, "ai", note=res.note), "ai_derived", used or ["profile"])

        # 7. truly unknown: ask you (once - your answer is remembered)
        note = missing.note if missing is not None else "No verified information available"
        return tag(Answer(None, 0.0, "none", needs_review=True, note=note), "ask", [])

    # ------------------------------------------------------------------ generation
    async def _generate(self, ctx: QuestionContext, intent: Intent) -> Answer | None:
        recognised = intent.category in GENERATIVE or intent.category == Category.EXPERIENCE
        cat = intent.category if recognised else Category.CUSTOM
        if real_options(ctx.options):
            return None  # a choice question is not a writing task
        budget = Budget.for_field(ctx.field_type, ctx.max_length, cat)
        if (budget.size != "short" or not recognised) and await self._ai_available():
            ai = await self._generate_with_ai(ctx, cat, budget, intent)
            if ai is not None:
                return ai
        if not recognised:
            return None  # the composer only writes answers to questions it understands; never a generic paragraph
        composed = self.composer.compose(cat, ctx.question, ctx.job, budget, intent.skill_focus)
        if composed is None or not composed.text.strip():
            return None
        return Answer(composed.text, composed.confidence, "generated",
                      note=composed.note or "written from your verified profile", mode="composed",
                      sources=composed.sources)

    async def _generate_with_ai(self, ctx: QuestionContext, cat: Category, budget: Budget,
                                intent: Intent) -> Answer | None:
        assert self.assistant is not None
        block = self.knowledge.prompt_block(ctx.job) + self._derived_block(intent)
        try:
            data = await generate_grounded(self.assistant, knowledge_block=block, category=cat.value,
                                           question=ctx.question, job=ctx.job, length=budget.size,
                                           max_chars=budget.max_chars, page_context=ctx.page_context)
        except (AIUnavailable, AIResponseError) as exc:
            log.info("Grounded generation failed (%s); using the profile composer", exc)
            return None
        text = re.sub(r"\s+", " ", str(data.get("answer") or "")).strip().strip('"')
        if data.get("requires_user") or not text:
            return None
        if re.search(r"\[[^\]]*\]|\{[^}]*\}|<[^>]+>|lorem ipsum|your\s+name|company\s+name", text, re.I):
            return None
        job_text = " ".join(x for x in [ctx.job.title, ctx.job.company, ctx.job.description] if x) if ctx.job else ""
        facts = self.profile.facts_for_ai() | {"derived": block}
        guard = check_text(text, facts, job_text + " " + (ctx.page_context or ""),
                           profile_skills=self.profile.skills.all)
        if not guard.ok:
            log.info("Generated answer rejected by the guard: %s", "; ".join(guard.violations))
            return None
        claims = check_claims(text, self.knowledge)
        if claims:
            log.info("Generated answer rejected (claims): %s", "; ".join(claims))
            return None
        keys = self.knowledge.keys()
        cited = [s for s in (data.get("sources") or []) if isinstance(s, str) and s in keys]
        sources = list(dict.fromkeys(attribute_sources(text, self.knowledge, job_text) + cited))
        try:
            conf = float(data.get("confidence") or 0.7)
        except (TypeError, ValueError):
            conf = 0.7
        conf = min(conf / 100 if conf > 1 else conf, 0.92) * guard.score
        if not any(not k.startswith("job.") for k in sources):
            conf = min(conf, 0.7)  # nothing in it traces back to your profile
        if conf < self.min_confidence:
            return None
        if len(text) > budget.max_chars:
            cut = text[: budget.max_chars]
            text = cut[: cut.rfind(".") + 1] if "." in cut else cut.rsplit(" ", 1)[0]
        note = "AI-written from your verified profile" + ("; guard: " + "; ".join(guard.warnings) if guard.warnings else "")
        return Answer(text, round(conf, 3), "ai", note=note, mode="ai_generated", sources=sources)

    def _derived_block(self, intent: Intent) -> str:
        lines = []
        for skill in intent.skill_focus[:3]:
            yrs, src = self.knowledge.skill_years(skill)
            if yrs is not None and yrs >= 0.5:
                from app.answers.knowledge import fmt_years

                lines.append(f"[derived.skill_years.{skill}] {fmt_years(yrs)} years with {skill} "
                             f"(from: {', '.join(src)})")
        return ("\n" + "\n".join(lines)) if lines else ""

    # ------------------------------------------------------------------ memory
    def _saved(self, key: str, ctx: QuestionContext) -> MemoryHit | None:
        """Your saved answer under ``key`` that may be used for this job: the most specific one that applies
        (this job, then this employer, then this portal, then your general answer)."""
        company = ctx.job.company if ctx.job else ""
        title = ctx.job.title if ctx.job else ""
        found = (self.memory_lookup(key, company, ctx.portal, title) if self._lookup_takes_job
                 else self.memory_lookup(key))
        if found is None:
            return None
        hits = [MemoryHit(h) if isinstance(h, str) else h for h in (found if isinstance(found, list) else [found])]
        c, t = _norm_company(company), _title_key(title)

        def applies(h: MemoryHit) -> bool:
            if h.scope == "job":
                return bool(t) and h.job_key == t and h.company == c
            if h.scope == "company":
                return not h.company or h.company == c  # an employer-specific answer stays with that employer
            if h.scope == "portal":  # e.g. "How did you find us on Naukri?" is not reused on LinkedIn
                return not h.portal or not ctx.portal or h.portal == ctx.portal
            return True

        order = {"job": 0, "company": 1, "portal": 2}
        usable = sorted((h for h in hits if applies(h)), key=lambda h: order.get(h.scope, 3))
        return usable[0] if usable else None

    def _memory(self, nq: str, ctx: QuestionContext, cat: Category) -> Answer | None:
        hit = self._saved(nq, ctx)
        if hit is None:
            return None
        if cat in CONTEXT_SPECIFIC and hit.scope not in {"company", "job"}:
            return None
        return Answer(hit.answer, 0.97, "memory", note="your saved answer")

    def _keyed_memory(self, ctx: QuestionContext, cat: Category) -> Answer | None:
        """An answer you gave once is reused for every wording of that question: 'Years with MongoDB' (and 'How long
        have you used MongoDB?'), 'D.O.B' (and 'Date of birth'), 'Reason for change' (and 'Why are you leaving?')."""
        key = canonical_key(ctx.question)
        if key is None:
            return None
        hit = self._saved(key, ctx)
        if hit is None or not hit.answer:
            return None
        if not key.startswith("skill years: "):
            return Answer(hit.answer, 0.95, "memory", note=f"your saved answer ({key.split(': ', 1)[1]})")
        if cat not in {Category.EXPERIENCE, Category.UNKNOWN}:
            return None
        m = re.search(r"\d+(?:\.\d+)?", hit.answer)
        if not m:
            return None
        yrs = float(m.group(0))
        months = bool(re.search(r"\bmonths?\b", ctx.question, re.I)) and not re.search(r"years?\s+and\s+months",
                                                                                     ctx.question, re.I)
        shown = str(int(round(yrs * 12))) if months else (str(int(yrs)) if yrs.is_integer() else str(yrs))
        return Answer(shown, 0.95, "memory", numeric=yrs, note=f"your saved answer for {key.split(': ', 1)[1]} years")

    # ------------------------------------------------------------------ fit to the control
    def _finalize(self, ctx: QuestionContext, ans: Answer) -> Answer:
        opts = real_options(ctx.options)
        if ans.choices and ctx.field_type == "checkbox_group" and all(c in opts for c in ans.choices):
            ans.value = "; ".join(ans.choices)  # several options of this form, by their own text
        elif ans.value is not None and opts and ctx.field_type in {"select", "radio", "checkbox_group"}:
            picked = choose_option(ans.value, opts, numeric=ans.numeric, yes_no=ans.yes_no,
                                   unit_scale=ans.unit_scale)
            if picked is None and ans.value == "" and not ctx.required:
                picked = ""
            if picked is None:
                ans.note = f"{ans.note}; no option matches '{ans.value}'".strip("; ")
                ans.needs_review = True
                ans.confidence = min(ans.confidence, 0.3)
            else:
                ans.value = picked
        if ctx.field_type == "checkbox" and ans.value:
            v = ans.value.strip().lower()
            if v in YES_WORDS | {"true", "checked", "1"}:
                ans.value = "Yes"
            elif v in NO_WORDS | {"false", "unchecked", "0"}:
                ans.value = "No"
            else:
                ans.note = f"{ans.note}; '{ans.value}' is not a yes/no answer".strip("; ")
                ans.needs_review = True
                ans.confidence = min(ans.confidence, 0.3)
        if ctx.max_length and ans.value and len(ans.value) > ctx.max_length:
            cut = ans.value[: ctx.max_length]
            ans.value = cut[: cut.rfind(".") + 1] if ans.mode in {"composed", "ai_generated"} and "." in cut else cut
        if ans.value is None or ans.confidence < self.min_confidence:
            ans.needs_review = True
        if ans.value == "" and ctx.required and ans.mode != "not_a_question":
            ans.needs_review = True
        if ans.needs_review and ans.mode not in {"ask"}:
            ans.mode = ans.mode or "ask"
        return ans


def _norm_company(name: str) -> str:
    n = re.sub(r"\b(?:pvt|private|ltd|limited|llp|inc|corp|corporation|technologies|solutions|services|india)\b\.?", "",
               (name or "").lower())
    return re.sub(r"[^a-z0-9]+", " ", n).strip()


_PORTAL_NAMES = re.compile(r"\b(naukri|linked\s*in|instahyre|indeed|monster|foundit|glassdoor|wellfound|iimjobs|hirist)\b",
                           re.I)


def _title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9+#]+", " ", (title or "").lower()).strip()[:255]


def skill_years_key(question: str) -> str | None:
    """'How many years of experience do you have with MongoDB?' -> 'skill years: mongodb' (one skill only).
    Also the years of a discipline or topic that is not a skill of yours ('as a Full Stack Developer' ->
    'skill years: full stack', 'with Relational Databases' -> 'skill years: relational databases')."""
    from app.answers.intent import question_skills
    from app.answers.question_bank import _GENERAL_TOPIC, _OTHER_DISCIPLINE, _WITH_TOPIC, repair_label

    if not re.search(r"\byears?\b|\bmonths?\b|how\s+long|experience\s+(?:in|with)", question or "", re.I):
        return None
    skills = question_skills(question)
    if len(skills) == 1:
        return f"skill years: {skills[0].lower()}"
    if skills:
        return None
    q = repair_label(question)
    if re.search(r"(?:total|overall)\s+(?:work\s+|professional\s+|it\s+|industry\s+)?(?:years\s+of\s+)?experience"
                 r"(?!\s+(?:in|with|on|using|of|as|working\s+with)\b)", q):
        return None  # your overall experience is a profile fact
    if (topic := _WITH_TOPIC.search(q)) and not _GENERAL_TOPIC.search(topic.group(1)):
        return f"skill years: {re.sub(r'[^a-z0-9+#]+', ' ', topic.group(1)).strip()[:60]}"
    if other := _OTHER_DISCIPLINE.search(q):
        return f"skill years: {re.sub(r'[^a-z0-9+#]+', ' ', other.group(0)).strip()}"
    return None


# Personal facts the profile has no field for. Asked once; the answer is then reused for every wording.
_FACT_KEYS: list[tuple[str, re.Pattern[str]]] = [(k, re.compile(rx, re.I)) for k, rx in [
    ("date of birth", r"date\s+of\s+birth|\bdob\b|birth\s*date|\bbirthday\b"),
    ("age", r"^(?:your\s+)?age(?:\s*\(?in\s+years\)?)?$|what\s+is\s+your\s+age|how\s+old\s+are\s+you"),
    ("hometown", r"home\s*town|native\s+(?:place|city|town)"),
    ("pan number", r"\bpan\s*(?:card\s*)?(?:number|no\b\.?|#)|\bpan\s+card\b|^pan$"),
    ("aadhaar number", r"aadha?ar"),
    ("passport number", r"passport\s*(?:number|no\b\.?|#)"),
    ("uan", r"\buan\b|universal\s+account\s+number"),
    ("father's name", r"father'?s?\s+name"),
    ("mother's name", r"mother'?s?\s+name"),
    ("marital status", r"marital\s+status"),
    ("blood group", r"blood\s+group"),
    ("12th percentage", r"(?:\b12\s*th\b|\bxii(?:th)?\b|\bhsc\b|higher\s+secondary|senior\s+secondary|\bintermediate\b)"
                        r".*(?:percent\w*|marks|c?gpa|grade|score|%)"),
    ("10th percentage", r"(?:\b10\s*th\b|\bssc\b|\bmatric\w*|\bhigh\s+school\b|(?<!higher\s)(?<!senior\s)\bsecondary\b)"
                        r".*(?:percent\w*|marks|c?gpa|grade|score|%)"),
    ("current monthly salary", r"(?:current|present)\s+monthly\s+(?:\w+\s+){0,2}?(?:salary|pay|income|ctc)|"
                               r"monthly\s+(?:in[\s-]?hand|take[\s-]?home)|in[\s-]?hand\s+salary|take[\s-]?home\s+(?:salary|pay)"),
    ("reason for change", r"reasons?\s+for\s+(?:the\s+|a\s+|your\s+)?(?:job\s+)?(?:change|leaving|switch\w*|looking|quitting|"
                          r"resign\w*|relieving)|why\s+(?:are\s+you\s+|do\s+you\s+want\s+to\s+)?(?:leav(?:e|ing)|looking\s+for\s+"
                          r"(?:a\s+)?(?:job\s+)?change|chang(?:e|ing)\s+(?:your\s+)?jobs?|switch(?:ing)?\s+(?:your\s+)?jobs?)"),
    ("night shifts", r"\b(?:night|rotational|rotating)\s+shifts?\b"),
]]


def fact_key(question: str) -> str | None:
    """'D.O.B' / 'Date of birth (DD/MM/YYYY)' -> 'fact: date of birth' (a question in another currency is not it)."""
    from app.answers.question_bank import repair_label

    q = repair_label(question)
    if re.search(r"\busd\b|\$|dollars?|\beuros?\b|\beur\b|\bgbp\b", q):
        return None
    return next((f"fact: {key}" for key, rx in _FACT_KEYS if rx.search(q)), None)


def canonical_key(question: str) -> str | None:
    """The wording-independent key of a question that has one (years with one skill, a personal fact)."""
    return skill_years_key(question) or fact_key(question)


def memory_scope(category: str, question: str, company: str, portal: str = "") -> tuple[str, str]:
    """How widely a saved answer may be reused: employer-specific questions stay with that employer,
    questions naming a job portal stay with that portal, everything else is reused everywhere."""
    c = _norm_company(company)
    q = (question or "").lower()
    if _PORTAL_NAMES.search(q) and portal:
        return "portal", portal
    mentions_company = bool(c) and c in re.sub(r"[^a-z0-9]+", " ", q)
    dated = bool(re.search(r"\b\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)|"
                           r"\b(?:today|tomorrow|this\s+week)\b", q))
    if category in {c_.value for c_ in CONTEXT_SPECIFIC} or mentions_company or dated:
        return "company", c
    return "global", ""
