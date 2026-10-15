"""The decision rules on a labelled dataset (tests/decision_dataset.py): no relevant role skipped, no unsuitable role
applied to, every ambiguous posting left to you - and every decision explained by structured reason codes.

Background - the 2026-10-15 audit of run 76 (106 new listings: 77 skipped, 19 for your decision, 7 queued):
* 12 "AI Engineer" titles were skipped from the title alone as "data roles", unread;
* React + Node.js / Python full-stack roles (35-50 % frontend, the backend in your own skills) were "backend-heavy";
* generic titles over .NET / Java / PHP stacks were put to you instead of being left out;
* a WordPress / Shopify role and a "Freelance Web Designer & Developer" (two skill mentions) were queued to apply;
* "a company ... with over 25 years of experience in ESG research" was read as "requires 25+ years";
* skip reasons said "about 15 % of the description is frontend work" when the description named no frontend at all.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.matching.analyzer import JobInput, parse_experience
from app.matching.matcher import JobMatcher
from app.matching.posting import Window
from app.matching.reasons import code_of, status_code
from app.matching.rules import title_prescreen
from app.models import Decision, utcnow
from tests.decision_dataset import CASES, Case


def decide(profile, case: Case):
    early = title_prescreen(profile, case.title)
    if early:
        return "SKIP", None, early
    r = JobMatcher(profile, None).evaluate_rules_only(JobInput(title=case.title, description=case.description,
                                                               location=case.location,
                                                               experience_text=case.experience_text))
    return r.decision, r, r.blockers + r.flags


def _correct(case: Case, decision: str) -> bool:
    if case.label == "relevant":
        return decision == "APPLY" or (case.review_ok and decision == "MANUAL_REVIEW")
    if case.label == "unsuitable":
        return decision != "APPLY"
    return decision == "MANUAL_REVIEW"


@pytest.mark.parametrize("case", CASES, ids=[c.key for c in CASES])
def test_each_case_is_decided_correctly_and_says_why(profile, case):
    decision, result, why = decide(profile, case)
    assert _correct(case, decision), (case.key, decision, why)
    if decision != "APPLY":
        assert why, f"{case.key}: {decision} without a stated reason"
        if result is not None:
            assert result.to_dict()["codes"], case.key  # a structured code for every blocker / flag


def test_rates_on_the_dataset(profile, capsys):
    relevant = [c for c in CASES if c.label == "relevant"]
    unsuitable = [c for c in CASES if c.label == "unsuitable"]
    false_skips = [c.key for c in relevant if decide(profile, c)[0] == "SKIP"]
    false_positives = [c.key for c in unsuitable if decide(profile, c)[0] == "APPLY"]
    left_to_you = [c.key for c in relevant if decide(profile, c)[0] == "MANUAL_REVIEW"]
    with capsys.disabled():
        print(f"\ndecision dataset: {len(CASES)} cases | false skips {len(false_skips)}/{len(relevant)} relevant | "
              f"false positives {len(false_positives)}/{len(unsuitable)} unsuitable | relevant left to you: "
              f"{len(left_to_you)} {left_to_you}")
    assert false_skips == [] and false_positives == []


def _by_key(key: str) -> Case:
    return next(c for c in CASES if c.key == key)


def test_an_ai_engineer_title_is_read_not_skipped_unread(profile):
    assert title_prescreen(profile, "Senior Forward Deployed AI Engineer") == []
    assert title_prescreen(profile, "Generative AI Engineer") == []
    assert title_prescreen(profile, "Machine Learning Engineer")  # (excluded by your keywords)
    assert decide(profile, _by_key("ai-engineer-ml"))[0] != "APPLY"  # read, then judged on what it is


def test_a_react_node_full_stack_role_is_in_your_own_stack(profile):
    decision, r, _ = decide(profile, _by_key("fullstack-react-node"))
    assert decision == "APPLY" and any("backend is in your own stack (Express, Node.js)" in x or
                                       "backend is in your own stack" in x for x in r.reasons), r.reasons
    # a backend you do not list (Python / Django here) stays backend-heavy - and says which skills are not yours
    decision, r, why = decide(profile, _by_key("django-fullstack"))
    assert decision != "APPLY"


def test_a_cms_role_and_a_freelance_designer_are_never_queued(profile):
    _, r, _ = decide(profile, _by_key("wordpress"))
    assert [c["code"] for c in r.to_dict()["codes"]][0] == "CMS_ROLE"
    _, r, why = decide(profile, _by_key("freelance-designer"))
    codes = {c["code"] for c in r.to_dict()["codes"]}
    assert {"EMPLOYMENT_TERMS", "DESIGN_ROLE"} <= codes, why


def test_contract_terms_are_yours_to_decide_even_for_a_perfect_match(profile):
    decision, r, _ = decide(profile, _by_key("react-contract"))
    assert decision == "MANUAL_REVIEW" and [c["code"] for c in r.to_dict()["codes"]] == ["EMPLOYMENT_TERMS"]


@pytest.mark.parametrize("text,expected", [
    ("Sustainalytics is a Morningstar company dedicated to responsible investment with over 25 years of experience in ESG "
     "research and ratings.", (None, None)),
    ("For over 20 years, Microsoft and its partners have recommended ALIANDO, for licensing and consulting.", (None, None)),
    ("Our team has 20+ years of combined experience. You bring 4+ years with TypeScript.", (4.0, None)),
    ("We value 8+ years of experience with Node.js and React.", (8.0, None)),  # names technologies: about you
    ("Our client is hiring a Senior Frontend Engineer with 7+ years of experience.", (7.0, None)),
    ("We are looking for someone with 6+ years of experience.", (6.0, None)),
])
def test_years_that_describe_the_employer_are_not_a_requirement(text, expected):
    assert parse_experience(text) == expected


def test_a_skip_says_what_the_description_names_not_an_invented_share(profile):
    _, r, _ = decide(profile, _by_key("dotnet-generic"))
    text = " ".join(r.blockers)
    assert "the description names no frontend technology" in text or "frontend:" in text
    assert "of the description is frontend work" not in text  # (the old wording, true only by coincidence)


@pytest.mark.parametrize("text,code", [
    ("Title contains excluded keyword 'devops'", "TITLE_EXCLUDED_KEYWORD"),
    ("Title looks like a data role", "TITLE_OTHER_FAMILY"),
    ("Requires 10+ years; you have 7", "EXPERIENCE_ABOVE"),
    ("Role targets 0-2 years of experience; you have 7", "EXPERIENCE_BELOW"),
    ("Full-stack role is backend-heavy (frontend ≈ 40%)", "FULLSTACK_BACKEND_HEAVY"),
    ("Employment terms are yours to decide: contract", "EMPLOYMENT_TERMS"),
    ("Something nobody expected", "OTHER"),
])
def test_reason_codes(text, code):
    assert code_of(text) == code


@pytest.mark.parametrize("status,reason,details,code", [
    ("SKIPPED", "Posted on 2026-06-25 (111 days ago, the portal's date): outside your job-recency window", {}, "TOO_OLD"),
    ("REVIEW_PENDING", "Relevant (score 90) - but posting date unknown - not applied automatically", {}, "DATE_UNKNOWN"),
    ("EXPIRED", "Closed: the listing's own end date (2026-09-23) has passed", {}, "CLOSED"),
    ("DUPLICATE", "Same role already handled via linkedin (job #2385)", {}, "DUPLICATE"),
    ("SKIPPED", "Not a frontend role", {"blockers": ["Not a frontend role: frontend focus ≈ 15%"]}, "FRONTEND_SHARE_LOW"),
    ("SKIPPED", "Low match (40.1)", {"blockers": [], "flags": []}, "LOW_SCORE"),
])
def test_status_codes_say_why_a_job_ended_where_it_is(status, reason, details, code):
    assert status_code(status, reason, details) == code


def test_old_and_undated_postings_of_a_relevant_role(profile):
    """A relevant role posted outside the window is skipped; one without a posting date waits for you."""
    from app.engine.recency import adjust

    old = SimpleNamespace(posted_at=utcnow() - timedelta(days=40), posted_open_ended=False, posted_date_only=False,
                          posted_source="page", posted_spread_s=0, posted_text="", discovered_at=utcnow(), valid_through=None)
    undated = SimpleNamespace(**{**vars(old), "posted_at": None})
    assert adjust(Decision.APPLY, "Relevant (score 95)", old, Window(days=7))[0] == Decision.SKIP
    assert adjust(Decision.APPLY, "Relevant (score 95)", undated, Window(days=7))[0] == Decision.MANUAL_REVIEW


def test_a_duplicate_listing_of_an_applied_role_is_recognised(clean_db):
    from app.db import session_scope
    from app.engine.recorder import find_duplicate, set_status, upsert_job
    from app.models import JobStatus
    from app.portals.types import JobPosting

    with session_scope() as s:
        first, _ = upsert_job(s, JobPosting(portal="linkedin", external_id="d-1", url="https://linkedin.test/d-1",
                                            title="Senior Frontend Engineer", company="Acme Labs"))
        set_status(s, first, JobStatus.APPLIED, "Applied with LinkedIn Easy Apply")
        again, _ = upsert_job(s, JobPosting(portal="naukri", external_id="d-2", url="https://naukri.test/d-2",
                                            title="Senior Frontend Engineer", company="Acme Labs"))
        assert find_duplicate(s, again) is first
