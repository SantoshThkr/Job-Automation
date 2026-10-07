"""Job matcher: deterministic rules first, then (optionally) the local AI for ambiguous cases.

The AI can refine the score and resolve MANUAL_REVIEW cases, but it can never override a hard
blocker (excluded keyword, experience gap, location, ...). Without Ollama the rules decide alone.
"""

from __future__ import annotations

import logging

from app.ai.job_assistant import AIResponseError, AIUnavailable, JobAssistant
from app.matching.analyzer import JobInput, analyze_job
from app.matching.rules import MatchResult, evaluate_rules
from app.models import Decision
from app.profile import Profile
from app.resumes import select_resume

log = logging.getLogger(__name__)

AI_WEIGHT = 0.3
AI_MIN_CONFIDENCE = 0.6
AI_DECISIVE_CONFIDENCE = 0.75


class JobMatcher:
    def __init__(self, profile: Profile, assistant: JobAssistant | None = None) -> None:
        self.profile = profile
        self.assistant = assistant

    def evaluate_rules_only(self, job: JobInput) -> MatchResult:
        result = evaluate_rules(self.profile, job, analyze_job(job))
        self._attach_resume(result)
        return result

    async def evaluate(self, job: JobInput, use_ai: bool | None = None) -> MatchResult:
        result = evaluate_rules(self.profile, job, analyze_job(job))
        m = self.profile.matching
        want_ai = m.use_ai if use_ai is None else use_ai
        if want_ai and self.assistant is not None and not result.blockers:
            near_threshold = (abs(result.score - m.apply_threshold) <= 10
                              or abs(result.score - m.skip_threshold) <= 5)
            if not m.ai_for_ambiguous_only or result.decision == Decision.MANUAL_REVIEW or near_threshold:
                await self._apply_ai(job, result)
        elif want_ai and result.blockers:
            result.ai = {"used": False, "reason": "hard blocker - AI not needed"}
        else:
            result.ai = {"used": False, "reason": "AI disabled for matching"}
        if result.ai is None:
            result.ai = {"used": False, "reason": "rules were decisive"}
        self._attach_resume(result)
        return result

    async def _apply_ai(self, job: JobInput, result: MatchResult) -> None:
        assert self.assistant is not None
        if not await self.assistant.available():
            result.ai = {"used": False, "reason": "Ollama unavailable - rules only"}
            return
        try:
            u = await self.assistant.understand_job(self.profile, job)
        except (AIUnavailable, AIResponseError) as exc:
            log.warning("AI job analysis failed: %s", exc)
            result.ai = {"used": False, "reason": f"AI failed: {exc}"}
            return

        info = {"used": True, **u.model_dump()}
        result.ai = info
        if u.confidence < AI_MIN_CONFIDENCE:
            info["effect"] = "ignored (low confidence)"
            return

        m = self.profile.matching
        rule_score = result.rule_score
        result.score = round((1 - AI_WEIGHT) * rule_score + AI_WEIGHT * u.relevance_score, 1)
        if result.frontend_ratio is not None:
            result.frontend_ratio = round(0.6 * result.frontend_ratio + 0.4 * u.frontend_percentage / 100, 2)
        else:
            result.frontend_ratio = round(u.frontend_percentage / 100, 2)

        before = result.decision
        decisive = u.confidence >= AI_DECISIVE_CONFIDENCE
        if before == Decision.APPLY:
            if decisive and u.recommendation == "SKIP":
                result.decision = Decision.MANUAL_REVIEW.value
                result.flags.append(f"AI disagrees with rules: {u.summary or 'not a fit'}")
            elif result.score < m.apply_threshold - 5:
                result.decision = Decision.MANUAL_REVIEW.value
        elif before == Decision.MANUAL_REVIEW:
            only_soft = all(not f.startswith("Framework mismatch") for f in result.flags)
            if decisive and u.recommendation == "APPLY" and u.is_frontend_role and only_soft \
                    and result.score >= m.apply_threshold - 5:
                result.decision = Decision.APPLY.value
            elif decisive and u.recommendation == "SKIP" and result.score < m.apply_threshold:
                result.decision = Decision.SKIP.value
        elif before == Decision.SKIP:
            if result.score >= m.skip_threshold and u.recommendation != "SKIP":
                result.decision = Decision.MANUAL_REVIEW.value
        info["effect"] = f"{before} -> {result.decision}" if before != result.decision else "confirmed"
        result.reasons.append(f"AI: {u.summary} (relevance {u.relevance_score}, "
                              f"frontend {u.frontend_percentage}%, confidence {u.confidence:.2f})")

    def _attach_resume(self, result: MatchResult) -> None:
        resume = select_resume(self.profile, result.category, result.matched_skills)
        result.resume_id = resume.id if resume else None
