"""Answer application questions: user memory -> custom answers -> profile rules -> AI -> manual review."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable

from app.ai.job_assistant import AIUnavailable, JobAssistant
from app.answers.options import NO_WORDS, YES_WORDS, choose_option, real_options
from app.answers.question_bank import OPEN_ENDED, Answer, QuestionContext, normalize_question, resolve
from app.config import get_settings
from app.profile import Profile

log = logging.getLogger(__name__)

MemoryLookup = Callable[[str], "str | None"]


class Answerer:
    def __init__(self, profile: Profile, assistant: JobAssistant | None = None,
                 memory_lookup: MemoryLookup | None = None, min_confidence: float | None = None,
                 allow_ai: bool = True) -> None:
        self.profile = profile
        self.assistant = assistant
        self.memory_lookup = memory_lookup or (lambda _q: None)
        self.min_confidence = get_settings().ai_min_confidence if min_confidence is None else min_confidence
        self.allow_ai = allow_ai

    async def answer(self, ctx: QuestionContext) -> Answer:
        try:
            ans = await self._answer(ctx)
        except Exception as exc:  # never let one question crash an application
            log.exception("Answering failed for %r", ctx.question)
            ans = Answer(None, 0.0, "error", needs_review=True, note=f"error: {exc}")
        return self._finalize(ctx, ans)

    async def _answer(self, ctx: QuestionContext) -> Answer:
        nq = normalize_question(ctx.question)

        remembered = self.memory_lookup(nq)
        if remembered is not None:
            return Answer(remembered, 0.97, "memory", note="your saved answer")

        for ca in self.profile.custom_answers:
            pat = ca.pattern.strip()
            if not pat:
                continue
            hit = re.search(pat[3:], nq, re.I) if pat.startswith("re:") else pat.lower() in nq
            if hit:
                return Answer(ca.answer, 0.95, "custom", note=f"custom answer '{pat}'")

        det = resolve(ctx, self.profile)
        if det is OPEN_ENDED or (det is not None and det.open_ended):
            return await self._open_ended(ctx)
        if det is not None:
            return det
        if not ctx.required:
            # never guess optional questions (newsletters, "follow company", optional surveys...)
            return Answer("", 0.9, "derived", note="optional question with no profile fact - left blank")

        if self.allow_ai and self.assistant is not None and await self.assistant.available():
            try:
                res = await self.assistant.answer_factual(self.profile, ctx.question, ctx.field_type,
                                                          real_options(ctx.options), ctx.job)
            except AIUnavailable as exc:
                return Answer(None, 0.0, "none", needs_review=True, note=f"unknown question; AI unavailable ({exc})")
            if res.answer:
                return Answer(res.answer, res.confidence, "ai", note=res.note)
            return Answer(None, 0.0, "ai", needs_review=True, note=res.note or "AI could not answer from profile")
        return Answer(None, 0.0, "none", needs_review=True, note="unknown question and no profile fact")

    async def _open_ended(self, ctx: QuestionContext) -> Answer:
        if not ctx.required and not re.search(r"why|interest|motivat|cover", ctx.question, re.I):
            return Answer("", 0.9, "derived", note="optional free-text question left blank")
        if not (self.allow_ai and self.assistant is not None and await self.assistant.available()):
            return Answer(None, 0.0, "none", needs_review=True, note="free-text answer needs AI or manual input")
        max_words = 120 if ctx.field_type == "textarea" or (ctx.max_length or 0) > 400 else 60
        if re.search(r"cover\s+letter", ctx.question, re.I):
            max_words = 220
        try:
            res = await self.assistant.write_answer(self.profile, ctx.question, ctx.job, max_words=max_words,
                                                    max_chars=ctx.max_length)
        except AIUnavailable as exc:
            return Answer(None, 0.0, "none", needs_review=True, note=f"AI unavailable ({exc})")
        if not res.answer:
            return Answer(None, 0.0, "ai", needs_review=True, note=res.note)
        return Answer(res.answer, res.confidence, "ai", note=res.note)

    def _finalize(self, ctx: QuestionContext, ans: Answer) -> Answer:
        opts = real_options(ctx.options)
        if ans.value is not None and opts and ctx.field_type in {"select", "radio", "checkbox_group"}:
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
            ans.value = ans.value[: ctx.max_length]
        if ans.value is None or ans.confidence < self.min_confidence:
            ans.needs_review = True
        if ans.value == "" and ctx.required:
            ans.needs_review = True
        return ans
