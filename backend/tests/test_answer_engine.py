"""The central answer engine: known -> derived -> generated -> saved -> ask (only when truly unknown).

Uses the test candidate (Asha Verma, 7 years, React/TypeScript/Next.js; Acme Fintech since 2021-04,
Pixel Labs 2017-07..2021-03). The local AI is off unless a test provides a fake model, so every
"generated" answer here comes from the deterministic grounded composer.
"""

from __future__ import annotations

import re

import pytest

from app.ai.guard import check_text
from app.ai.job_assistant import JobAssistant
from app.answers.answerer import Answerer, MemoryHit
from app.answers.grounding import check_claims
from app.answers.intent import Category, classify
from app.answers.knowledge import CandidateKnowledge
from app.answers.question_bank import QuestionContext
from app.matching.analyzer import JobInput
from tests.conftest import fake_ollama

JOB = JobInput(title="Senior React / Next.js Engineer", company="Razorpay", location="Bengaluru",
               description="Build React and Next.js payment UIs with TypeScript. Frontend architecture, web performance "
                           "(Core Web Vitals) and accessibility. 6+ years of experience.")


def q(question, field_type="text", options=None, required=True, max_length=None, job=JOB):
    return QuestionContext(question=question, field_type=field_type, options=options or [], required=required,
                           job=job, portal="naukri", max_length=max_length)


@pytest.fixture
def candidate(profile):
    p = profile.model_copy(deep=True)
    p.professional.last_working_day = "15 November 2026"
    p.professional.serving_notice = True
    return p


@pytest.fixture
def engine(candidate):
    return Answerer(candidate, None, min_confidence=0.75)


def sentences(text: str) -> int:
    return len(re.findall(r"[.!?](?:\s|$)", text))


def grounded(text: str, profile) -> bool:
    guard = check_text(text, profile.facts_for_ai(), JOB.title + " " + JOB.company + " " + JOB.description,
                       profile_skills=profile.skills.all)
    return guard.ok and not check_claims(text, CandidateKnowledge(profile))


# ------------------------------------------------------------------ 1. direct facts

@pytest.mark.parametrize("question,field_type,expected", [
    ("Current city", "text", "Bengaluru"),
    ("Current company", "text", "Acme Fintech"),
    ("What is your current CTC?", "text", "28 LPA"),
    ("Expected CTC (in LPA)", "text", "38"),
    ("What is your last working day?", "text", "15 November 2026"),
    ("Last working day", "date", "2026-11-15"),
])
async def test_direct_facts(engine, question, field_type, expected):
    a = await engine.answer(q(question, field_type))
    assert a.value == expected and a.mode == "profile" and not a.needs_review, (a.value, a.note)
    assert a.sources and all(s.startswith(("personal.", "professional.")) for s in a.sources)


# ------------------------------------------------------------------ 2. derived facts

async def test_notice_period_and_joining(engine):
    a = await engine.answer(q("What is your notice period?", "select", ["Immediate", "15 days", "1 month", "2 months"]))
    assert a.value == "1 month"
    a = await engine.answer(q("How soon can you join?"))
    assert a.value == "After my last working day (15 November 2026)" and a.mode == "derived"
    a = await engine.answer(q("Earliest possible start date", "date"))
    assert a.value == "2026-11-16"


async def test_relocation_and_locations(engine):
    a = await engine.answer(q("Are you willing to relocate to Bengaluru?", "radio", ["Yes", "No"]))
    assert a.value == "Yes"  # already your city
    a = await engine.answer(q("Please select the city you are currently residing or willing to relocate to", "radio",
                              ["Chennai", "Bangalore", "Pune"]))
    assert a.value == "Bangalore" and a.mode == "derived"  # a city, not "Yes"
    a = await engine.answer(q("Preferred work locations"))
    assert "Bengaluru" in a.value


async def test_experience_years_explicit_and_derived(engine):
    a = await engine.answer(q("How many years of React experience do you have?", "number"))
    assert a.value == "6"  # skills.years
    a = await engine.answer(q("Years of experience with Redux", "number"))
    assert a.value == "3.5" and a.mode == "derived"  # Pixel Labs 2017-07 .. 2021-03 lists Redux
    a = await engine.answer(q("Total years of experience", "select", ["0-3", "4-6", "7-10", "10+"]))
    assert a.value == "7-10"


# ------------------------------------------------------------------ 3. generated answers

GENERATED = [
    ("Why are you interested in this role?", Category.WHY_ROLE, ["Razorpay", "React"]),
    ("Why should we hire you?", Category.WHY_HIRE, ["7 years", "React"]),
    ("Describe your React experience.", Category.TECHNICAL_EXPERIENCE, ["6 years", "React", "Acme Fintech"]),
    ("Describe your Next.js experience.", Category.TECHNICAL_EXPERIENCE, ["Next.js", "Acme Fintech"]),
    ("What are your strengths?", Category.STRENGTHS, ["React"]),
    ("What are your current responsibilities?", Category.CURRENT_RESPONSIBILITIES, ["Acme Fintech", "checkout"]),
    ("Why are you a good fit?", Category.WHY_HIRE, ["React"]),
]


@pytest.mark.parametrize("question,category,must_contain", GENERATED)
async def test_generated_answers_are_automatic_and_grounded(engine, candidate, question, category, must_contain):
    a = await engine.answer(q(question, "textarea"))
    assert a.category == category.value
    assert a.value and not a.needs_review and a.mode == "composed", (a.value, a.note)
    for text in must_contain:
        assert text in a.value, (text, a.value)
    assert grounded(a.value, candidate), a.value
    assert a.sources and a.confidence >= 0.75
    assert 1 <= sentences(a.value) <= 4 and len(a.value) <= 600  # a few concise sentences


async def test_answer_length_follows_the_field(engine):
    short = await engine.answer(q("Why are you a good fit?", "text"))
    long = await engine.answer(q("Why are you a good fit?", "textarea"))
    assert len(short.value) <= 160 and sentences(short.value) == 1
    assert len(long.value) > len(short.value)
    tiny = await engine.answer(q("Why are you interested in this role?", "textarea", max_length=80))
    assert len(tiny.value) <= 80
    letter = await engine.answer(q("Cover letter", "textarea"))
    assert len(letter.value) > len(long.value) and "Asha Verma" in letter.value


async def test_achievement_comes_only_from_a_stated_outcome(engine, candidate):
    a = await engine.answer(q("What is your biggest professional achievement?", "textarea"))
    assert "40%" in a.value and not a.needs_review  # "Improved Largest Contentful Paint by 40% ..." highlight
    bare = candidate.model_copy(deep=True)
    bare.professional.highlights = []
    a = await Answerer(bare).answer(q("What is your biggest professional achievement?", "textarea"))
    assert a.value is None and a.needs_review and a.mode == "ask"


# ------------------------------------------------------------------ 4. truly unknown -> ask you

@pytest.mark.parametrize("question,field_type,options", [
    ("Do you have a valid US work visa?", "radio", ["Yes", "No"]),
    ("What is your passport number?", "text", None),
    ("Have you worked for Company X?", "radio", ["Yes", "No"]),
    ("Virtual interview availability for 8th Oct?", "text", None),
])
async def test_truly_unknown_asks_you(engine, question, field_type, options):
    a = await engine.answer(q(question, field_type, options))
    assert a.value is None and a.needs_review and a.mode == "ask", (a.value, a.note)


async def test_known_employer_is_derived_not_asked(engine):
    a = await engine.answer(q("Have you previously worked with Pixel Labs?", "radio", ["Yes", "No"]))
    assert a.value == "Yes" and not a.needs_review


async def test_chatbot_messages_are_not_questions(engine):
    for msg in ["Thank you for your responses.",
                "Hi Asha Verma, thank you for showing interest. Kindly answer all the recruiter's questions."]:
        a = await engine.answer(q(msg))
        assert a.mode == "not_a_question" and a.value == ""


# ------------------------------------------------------------------ semantic understanding

@pytest.mark.parametrize("question", ["Why are you interested in this job?", "Why do you want to join this position?",
                                      "What attracts you to this role?", "Why are you applying for this role?"])
def test_why_role_paraphrases(question):
    assert classify(question, field_type="textarea").category == Category.WHY_ROLE


@pytest.mark.parametrize("question,field_type,expected", [
    ("Tell us about your React experience", "textarea", Category.TECHNICAL_EXPERIENCE),
    ("How experienced are you with React?", "textarea", Category.TECHNICAL_EXPERIENCE),
    ("Describe your React background", "textarea", Category.TECHNICAL_EXPERIENCE),
    ("Years of experience in React", "number", Category.EXPERIENCE),
])
def test_react_experience_paraphrases(question, field_type, expected):
    intent = classify(question, field_type=field_type)
    assert intent.category == expected and intent.skill_focus == ["React"]


# ------------------------------------------------------------------ priorities and memory

async def test_profile_fact_beats_a_saved_answer(candidate):
    engine = Answerer(candidate, memory_lookup=lambda nq: MemoryHit("Mumbai"))
    a = await engine.answer(q("Current city"))
    assert a.value == "Bengaluru"


async def test_saved_answer_used_when_nothing_else_knows(candidate):
    engine = Answerer(candidate, memory_lookup=lambda nq: MemoryHit("Yes") if "passport" in nq else None)
    a = await engine.answer(q("Do you hold a valid passport?", "radio", ["Yes", "No"]))
    assert a.value == "Yes" and a.mode == "memory"


async def test_company_specific_answer_is_not_reused_elsewhere(candidate):
    saved = MemoryHit("I love Razorpay's payments work", scope="company", company="razorpay")
    engine = Answerer(candidate, memory_lookup=lambda nq: saved if "why do you want to work" in nq else None)
    other = JobInput(title="Frontend Engineer", company="Acme Cloud", description="React TypeScript")
    a = await engine.answer(q("Why do you want to work here?", "textarea", job=other))
    assert a.value and "Razorpay" not in a.value  # generated for Acme Cloud instead


# ------------------------------------------------------------------ local AI path (validated)

async def test_grounded_ai_answer_is_used_with_attributed_sources(candidate):
    text = ("I have built React and Next.js checkout experiences at Acme Fintech with TypeScript, and the role's "
            "focus on web performance and accessibility matches that work.")
    provider = fake_ollama([{"answer": text, "sources": ["job.description"], "confidence": 0.9,
                             "requires_user": False}])
    a = await Answerer(candidate, JobAssistant(provider)).answer(q("Why are you interested in this role?", "textarea"))
    assert a.value == text and a.mode == "ai_generated"
    assert "professional.work_history[0]" in a.sources  # attributed from the content, not the model's citation


async def test_ai_crediting_skill_to_wrong_employer_is_rejected(candidate):
    text = "At Pixel Labs I built Next.js applications with TypeScript."  # Pixel Labs never listed Next.js
    provider = fake_ollama([{"answer": text, "sources": ["professional.work_history[1]"], "confidence": 0.95,
                             "requires_user": False}])
    a = await Answerer(candidate, JobAssistant(provider)).answer(q("Describe your Next.js experience.", "textarea"))
    assert a.value != text and a.mode == "composed" and not a.needs_review


async def test_ai_inflating_the_role_is_rejected(candidate):
    text = "At Acme Fintech I managed a team of engineers building React checkout flows."
    provider = fake_ollama([{"answer": text, "sources": ["professional.work_history[0]"], "confidence": 0.95,
                             "requires_user": False}])
    a = await Answerer(candidate, JobAssistant(provider)).answer(q("Why should we hire you?", "textarea"))
    assert "managed" not in a.value and a.mode == "composed"


def test_knowledge_derives_skill_years_from_dates(candidate):
    from datetime import date

    k = CandidateKnowledge(candidate, today=date(2026, 10, 7))
    yrs, src = k.skill_years("Redux")  # not in skills.years: Pixel Labs 2017-07 .. 2021-03 lists it
    assert 3.6 < yrs < 3.75 and src == ["professional.work_history[1]"]
    assert k.skill_years("React")[0] == 6  # explicit skills.years wins
    assert k.skill_years("Kubernetes") == (None, [])
