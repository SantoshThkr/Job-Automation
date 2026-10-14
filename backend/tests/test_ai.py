"""Phase 8: AI provider robustness (JSON repair, schema validation, availability) + optional live Ollama."""

from __future__ import annotations

import pytest

from app.ai.guard import check_text
from app.ai.job_assistant import JobAssistant, JobUnderstanding
from app.ai.provider import AIResponseError, AIUnavailable, OllamaProvider, extract_json
from app.matching.analyzer import JobInput
from tests.conftest import fake_ollama, ollama_is_running


@pytest.mark.parametrize("raw,expected", [
    ('{"a": 1}', {"a": 1}),
    ('```json\n{"a": 2}\n```', {"a": 2}),
    ('<think>reasoning {not json}</think>{"a": 3}', {"a": 3}),
    ('Sure! Here is the JSON: {"a": {"b": "x}"}} hope it helps', {"a": {"b": "x}"}}),
    ('{"a": [1, 2,], }', {"a": [1, 2]}),
])
def test_extract_json_repairs(raw, expected):
    assert extract_json(raw) == expected


def test_extract_json_rejects_garbage():
    with pytest.raises(AIResponseError):
        extract_json("no json here")


def test_understanding_validation_clamps_values():
    u = JobUnderstanding.model_validate({"is_frontend_role": True, "frontend_percentage": "85", "relevance_score": 130,
                                         "important_skills": [], "concerns": [], "recommendation": "review",
                                         "confidence": 85, "summary": ""})
    assert u.relevance_score == 100 and u.confidence == 0.85 and u.recommendation == "MANUAL_REVIEW"


async def test_understand_job_rejects_invalid_schema(profile):
    assistant = JobAssistant(fake_ollama([{"is_frontend_role": "maybe"}]))
    with pytest.raises(AIResponseError):
        await assistant.understand_job(profile, JobInput(title="React Dev", description="React"))


async def test_disabled_provider():
    p = OllamaProvider(enabled=False)
    assert not await p.is_available()
    with pytest.raises(AIUnavailable):
        await p.chat("s", "u")


async def test_unreachable_ollama():
    p = OllamaProvider(base_url="http://127.0.0.1:9", enabled=True, timeout=2)
    h = await p.health(use_cache=False)
    assert h["available"] is False and "not reachable" in h["error"]
    with pytest.raises(AIUnavailable):
        await p.chat("s", "u")


def test_guard_flags_invented_facts(profile):
    facts = profile.facts_for_ai()
    ok = check_text("I have 7 years of experience building React apps at Acme Fintech.", facts, "",
                    profile_skills=profile.skills.all)
    assert ok.ok, ok.violations
    bad = check_text("I have 11 years of experience with Kubernetes at Infosys.", facts, "",
                     profile_skills=profile.skills.all)
    assert not bad.ok
    assert any("11" in v for v in bad.violations)
    assert any("Infosys" in v for v in bad.violations)
    assert any("Kubernetes" in v for v in bad.violations)


@pytest.mark.live_ollama
@pytest.mark.skipif(not ollama_is_running(), reason="Ollama not running locally")
async def test_live_ollama_job_understanding_and_answer(profile):
    from app.config import get_settings

    from app.browser.resources import SWAP_HEADROOM_WARN_MB, memory_state, system_memory

    provider = OllamaProvider(base_url="http://localhost:11434", model=get_settings().ollama_model, enabled=True)
    if not await provider.is_available():
        pytest.skip("configured Ollama model not pulled")
    # This test loads the real model (about 3 GB). It follows the rule a run follows: not under memory pressure and
    # not without room for it - on 2026-10-13 it pushed a Mac that was already under pressure 1.3 GB further into swap.
    memory = system_memory()
    if memory_state(memory)[0] != "ok" or memory.swap_headroom_mb < SWAP_HEADROOM_WARN_MB + get_settings().ai_model_memory_mb:
        pytest.skip("not enough free memory to load the local model right now (the same rule a run applies)")
    assistant = JobAssistant(provider)
    job = JobInput(title="Senior Frontend Engineer", company="Razorpay", location="Bengaluru",
                   description="Build React + TypeScript dashboards. Requirements: 5+ years React, TypeScript, CSS, "
                               "Jest. Nice to have: Next.js.")
    try:
        u = await assistant.understand_job(profile, job)
        assert u.is_frontend_role is True
        assert u.frontend_percentage >= 60
        assert u.recommendation in {"APPLY", "MANUAL_REVIEW"}

        res = await assistant.write_answer(profile, "Why are you interested in this role?", job, max_words=80)
        assert res.answer and len(res.answer.split()) <= 85
        assert 0 <= res.confidence <= 1

        fact = await assistant.answer_factual(profile, "What is your passport number?", "text", [], job)
        assert fact.answer is None or fact.confidence < 0.75  # must not invent a passport number
    finally:
        await provider.unload()  # the model's memory is given back at once, not after its keep-alive
