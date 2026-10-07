"""Phase 2: deterministic job matching + AI refinement with realistic job descriptions."""

from __future__ import annotations

import httpx
import pytest

from app.ai.job_assistant import JobAssistant
from app.matching.analyzer import (
    JobInput,
    analyze_job,
    classify_title,
    normalize_city,
    parse_experience,
    parse_salary_lpa,
    title_seniority,
)
from app.matching.matcher import JobMatcher
from app.matching.skills import canonicalize, extract_skills
from tests.conftest import fake_ollama
from tests.fixtures.jds import JDS


def _job(jd: dict) -> JobInput:
    return JobInput(title=jd["title"], description=jd["description"], company=jd["company"],
                    location=jd["location"], experience_text=jd["experience_text"], skills=jd["skills"])


@pytest.mark.parametrize("jd", JDS, ids=[j["id"] for j in JDS])
def test_rules_classify_realistic_jds(profile, jd):
    result = JobMatcher(profile).evaluate_rules_only(_job(jd))
    expected = jd["expected"] if isinstance(jd["expected"], list) else [jd["expected"]]
    assert result.decision in expected, (result.decision, result.score, result.blockers, result.flags)
    assert result.reasons, "every decision must be explained"


def test_frontend_ratio_orders_roles(profile):
    by_id = {j["id"]: analyze_job(_job(j)).frontend_ratio for j in JDS}
    assert by_id["senior_react_bangalore"] > by_id["fullstack_mern"] > by_id["java_fullstack_pune"] > by_id["java_backend"]
    assert by_id["devops"] < 0.2


@pytest.mark.parametrize("text,expected", [
    ("5-9 Yrs", (5, 9)), ("5 - 8 years", (5, 8)), ("3 to 5 years", (3, 5)), ("8+ Yrs", (8, None)),
    ("Minimum 4 years of experience", (4, None)), ("at least 6 yrs", (6, None)),
    ("We need 5 years of experience in React and 3+ years TypeScript", (5, None)),
    ("Fresher", (0, 1)), ("", (None, None)), ("No experience info", (None, None)),
])
def test_parse_experience(text, expected):
    assert parse_experience(text) == expected


@pytest.mark.parametrize("title,cat,level", [
    ("Senior Frontend Engineer", "frontend", "senior"), ("Sr. React Developer", "frontend", "senior"),
    ("SDE-2 Frontend", "frontend", "senior"), ("SDE III", "generic", "lead"),
    ("Lead UI Engineer", "frontend", "lead"), ("Full Stack Developer (MERN)", "fullstack", "unspecified"),
    ("Java Developer", "backend", "unspecified"), ("QA Automation Engineer - JavaScript", "qa", "unspecified"),
    ("React Native Developer", "mobile", "unspecified"), ("Director of Engineering", "other", "director"),
    ("Associate Software Engineer", "generic", "junior"), ("Software Engineer", "generic", "unspecified"),
])
def test_title_classification(title, cat, level):
    assert classify_title(title) == cat
    assert title_seniority(title) == level


def test_skill_extraction_aliases():
    s = extract_skills("ReactJS, React.js, Next.js, NodeJS, Java (Spring Boot), JavaScript, TS, React Native")
    assert {"React", "Next.js", "Node.js", "Java", "Spring Boot", "JavaScript", "React Native"} <= set(s)
    assert canonicalize("reactjs") == "React"
    assert canonicalize("Node JS") == "Node.js"
    # "Java" must not fire on JavaScript
    assert "Java" not in extract_skills("Strong JavaScript skills")


def test_location_and_salary_parsing():
    assert normalize_city("Bangalore/Bengaluru") == "bengaluru"
    assert normalize_city("Gurugram") == "delhi ncr"
    assert parse_salary_lpa("12-18 Lacs P.A.") == (12, 18)
    assert parse_salary_lpa("₹ 15,00,000 - 25,00,000 P.A.") == (15, 25)
    assert parse_salary_lpa("Not disclosed") == (None, None)


def test_hard_blockers(profile):
    m = JobMatcher(profile)
    excluded_company = m.evaluate_rules_only(JobInput(title="React Developer", company="Blocked Corp Pvt Ltd",
                                                      location="Bengaluru", description="React TypeScript"))
    assert excluded_company.decision == "SKIP" and any("excluded" in b for b in excluded_company.blockers)
    remote = m.evaluate_rules_only(JobInput(title="Frontend Engineer", location="Remote", experience_text="5-8 yrs",
                                            description="Requirements: React, TypeScript, CSS, HTML, Redux"))
    assert remote.decision == "APPLY"


def test_salary_blocker(profile):
    p = profile.model_copy(deep=True)
    p.job_preferences.min_salary_lpa = 30
    r = JobMatcher(p).evaluate_rules_only(JobInput(title="React Developer", location="Bengaluru",
                                                   salary_text="10-15 Lacs P.A.", description="React, TypeScript"))
    assert r.decision == "SKIP" and any("Salary" in b for b in r.blockers)


# ---------------------------------------------------------------- AI layer ---

def _understanding(**kw):
    base = {"is_frontend_role": True, "frontend_percentage": 90, "relevance_score": 88, "important_skills": ["React"],
            "concerns": [], "recommendation": "APPLY", "confidence": 0.9, "summary": "Strong frontend fit"}
    base.update(kw)
    return base


async def test_ai_resolves_ambiguous_case(profile):
    p = profile.model_copy(deep=True)
    p.matching.use_ai = True
    # a generic title where rules land in MANUAL_REVIEW because of an unclear title
    job = JobInput(title="Member of Technical Staff", location="Bengaluru", experience_text="5-8 Yrs",
                   description="Requirements:\n- React, TypeScript, Redux, HTML, CSS\n- Jest\n- Node.js for BFF")
    rules = JobMatcher(p).evaluate_rules_only(job)
    assert rules.decision == "MANUAL_REVIEW"
    matcher = JobMatcher(p, JobAssistant(fake_ollama([_understanding(relevance_score=92)])))
    result = await matcher.evaluate(job)
    assert result.ai["used"] is True
    assert result.decision == "APPLY"


async def test_ai_cannot_override_blockers(profile):
    p = profile.model_copy(deep=True)
    p.matching.use_ai = True
    provider = fake_ollama([_understanding()])
    matcher = JobMatcher(p, JobAssistant(provider))
    jd = next(j for j in JDS if j["id"] == "ui_dev_hyderabad")
    result = await matcher.evaluate(_job(jd))
    assert result.decision == "SKIP"
    assert provider.calls == []  # AI not even consulted for hard blockers


async def test_ai_disagreement_downgrades_apply_to_review(profile):
    p = profile.model_copy(deep=True)
    p.matching.use_ai = True
    p.matching.ai_for_ambiguous_only = False
    jd = next(j for j in JDS if j["id"] == "senior_react_bangalore")
    matcher = JobMatcher(p, JobAssistant(fake_ollama([_understanding(recommendation="SKIP", relevance_score=20,
                                                                     confidence=0.9, summary="Actually backend")])))
    result = await matcher.evaluate(_job(jd))
    assert result.decision == "MANUAL_REVIEW"


async def test_matching_works_when_ollama_unavailable(profile):
    p = profile.model_copy(deep=True)
    p.matching.use_ai = True
    matcher = JobMatcher(p, JobAssistant(fake_ollama([], tags_ok=False)))
    jd = next(j for j in JDS if j["id"] == "angular_lead")
    result = await matcher.evaluate(_job(jd))
    assert result.decision == "MANUAL_REVIEW"
    assert result.ai["used"] is False and "unavailable" in result.ai["reason"].lower()


async def test_matching_survives_malformed_ai_response(profile):
    p = profile.model_copy(deep=True)
    p.matching.use_ai = True
    matcher = JobMatcher(p, JobAssistant(fake_ollama(["this is not json at all", "still not json"])))
    jd = next(j for j in JDS if j["id"] == "vue_frontend")
    result = await matcher.evaluate(_job(jd))
    assert result.decision == "MANUAL_REVIEW"
    assert result.ai["used"] is False and "failed" in result.ai["reason"]


async def test_low_confidence_ai_is_ignored(profile):
    p = profile.model_copy(deep=True)
    p.matching.use_ai = True
    jd = next(j for j in JDS if j["id"] == "vue_frontend")
    matcher = JobMatcher(p, JobAssistant(fake_ollama([_understanding(confidence=0.3)])))
    result = await matcher.evaluate(_job(jd))
    assert result.decision == "MANUAL_REVIEW"
    assert result.score == result.rule_score
    assert "low confidence" in result.ai["effect"]


async def test_ai_connection_error_opens_circuit(profile):
    from app.ai.provider import AIUnavailable

    provider = fake_ollama([httpx.ConnectError("boom")])
    with pytest.raises(AIUnavailable):
        await provider.chat("s", "p")
    assert not await provider.is_available()  # circuit open for 60s
