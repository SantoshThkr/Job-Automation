"""Job relevance: which postings are applied to, which are left out, and why.

Background (audit of 2026-10-13, 1 011 skipped jobs and 146 application attempts of that day):

* a "Senior Frontend Engineer" with a fit of 85 was shown as Skipped: its description asks for 10+ years - a hard
  requirement that overrides the score, shown without saying so;
* Naukri's experience field said "0-2 Yrs" on senior roles whose description asked for 5+ years: skipped as junior;
* "7 - 10 years of professional experience" was read as "10+ years" and "an established company with 10+ years of
  industry presence" as a requirement of the role;
* the local model had turned 145 jobs the rules had flagged into "apply" - 54 of them with less than half of the
  description about frontend work;
* fourteen strong matches skipped for their city stayed Skipped after those cities were added to the profile.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.ai.job_assistant import JobAssistant
from app.db import session_scope
from app.engine.reconsider import reconsider_skipped
from app.engine.recorder import save_match, set_status
from app.matching.analyzer import JobInput, parse_experience, posting_experience
from app.matching.matcher import JobMatcher
from app.matching.rules import decision_reason, preferences_rev, skip_reason, title_prescreen
from app.models import Application, ApplicationEvent, Job, JobStatus, utcnow
from tests.conftest import fake_ollama

FRONTEND_JD = ("We are hiring a frontend engineer.\nRequirements:\n- React, TypeScript, Redux, HTML, CSS\n- Next.js\n"
               "- Jest and React Testing Library\n- REST APIs, performance tuning, accessibility\n" * 2)
BACKEND_JD = ("You will build distributed backend services.\nRequirements:\n- Java, Spring Boot, Hibernate, Kafka\n"
              "- PostgreSQL, Redis, microservices\n- Kubernetes, Docker, AWS\n- gRPC, system design\n" * 2)


def job(title, description=FRONTEND_JD, experience="5-8 Yrs", location="Bengaluru", **kw) -> JobInput:
    return JobInput(title=title, description=description, experience_text=experience, location=location, **kw)


def rules(profile, *a, **kw):
    return JobMatcher(profile).evaluate_rules_only(job(*a, **kw))


# ------------------------------------------------------------------ the roles you want, and the ones you do not

@pytest.mark.parametrize("title", [
    "Senior Frontend Engineer", "Frontend Engineer", "Frontend Developer", "Senior React Developer",
    "React.js Developer", "Next.js Developer", "TypeScript Developer", "UI Engineer - React",
    "Senior Frontend Engineer (React/Next.js)", "Frontend Engineer - GenAI Products",
])
def test_frontend_roles_are_applied_to(profile, title):
    r = rules(profile, title)
    assert r.decision == "APPLY", (title, r.score, r.blockers, r.flags)
    assert r.reasons and r.profile_rev == preferences_rev(profile)


@pytest.mark.parametrize("title", ["Full Stack Engineer (React + Node.js)", "Fullstack Developer - React"])
def test_frontend_heavy_full_stack_roles_are_applied_to(profile, title):
    r = rules(profile, title, description=FRONTEND_JD + "\n- Node.js for the BFF layer")
    assert r.decision == "APPLY", (title, r.score, r.blockers, r.flags)


@pytest.mark.parametrize("title,description", [
    ("Java Developer", BACKEND_JD), ("Senior DevOps Engineer", BACKEND_JD), ("QA Engineer", FRONTEND_JD),
    ("SDET - Automation", FRONTEND_JD), ("Data Engineer", BACKEND_JD), ("Android Developer", FRONTEND_JD),
    ("React Native Developer", FRONTEND_JD), ("Backend Engineer", BACKEND_JD), ("Frontend Intern", FRONTEND_JD),
    ("Senior Software Engineer", BACKEND_JD),  # a generic title over a backend description
    ("Forward Deployed Engineer", BACKEND_JD), ("Full Stack Engineer", BACKEND_JD),
])
def test_unrelated_roles_are_skipped_with_a_reason_you_can_read(profile, title, description):
    early = title_prescreen(profile, title)
    r = rules(profile, title, description=description)
    assert early or r.decision == "SKIP", (title, r.decision, r.score, r.flags)
    reason = "; ".join(early) or decision_reason(r, profile.matching.apply_threshold)
    assert len(reason) > 15 and reason[0].isupper(), reason  # a sentence, not a code


def test_a_measured_backend_description_is_a_skip_not_a_decision(profile):
    """'Senior Software Engineer' / 'Full Stack Engineer' over a backend description used to wait in Needs decision
    (and the model then called it frontend). Far below your minimum, measured: it is left out, and says why."""
    for title, kind in (("Senior Software Engineer", "Not a frontend role"), ("Full Stack Engineer", "Backend-heavy full-stack role")):
        r = rules(profile, title, description=BACKEND_JD)
        assert r.decision == "SKIP" and any(b.startswith(kind) and "your minimum is 50%" in b for b in r.blockers), r.blockers
    # nothing measurable (a thin description) is not "measured backend": that one is yours to decide
    thin = rules(profile, "Senior Software Engineer", description="Great team, great product. Apply now!")
    assert thin.decision == "MANUAL_REVIEW" and not thin.blockers


# ------------------------------------------------------------------ a high score next to "Skipped"

def test_a_hard_requirement_explains_itself_next_to_a_high_score(profile):
    """LinkedIn 'Senior Frontend Engineer - Cortex Platform' (2026-10-13): fit 85, description "Experience: 10+
    Years", you have 7. Skipped - correctly - and the reason now says the requirement overrides the score."""
    r = rules(profile, "Senior Frontend Engineer – Cortex Platform", experience="",
              description=FRONTEND_JD + "\nExperience: 10+ Years\n10+ years of frontend or UI engineering experience")
    assert r.decision == "SKIP" and r.score >= profile.matching.apply_threshold
    why = skip_reason(r, profile.matching.apply_threshold)
    assert why.startswith("Requires 10+ years; you have 7") and "hard requirement" in why and f"{r.score:g}" in why
    low = rules(profile, "Java Developer", description=BACKEND_JD)
    assert "hard requirement" not in skip_reason(low, profile.matching.apply_threshold)  # nothing to explain there


def test_a_high_score_alone_never_applies(profile):
    """Every one of these scores above the apply threshold; none is applied to."""
    blocked = rules(profile, "Senior Frontend Engineer", experience="12-15 Yrs")
    elsewhere = rules(profile, "Senior Frontend Engineer", location="Kolkata")
    flagged = rules(profile, "Senior Frontend Engineer", description=FRONTEND_JD.replace("React", "Vue").replace("Next.js", "Nuxt"))
    for r in (blocked, elsewhere):
        assert r.score >= profile.matching.apply_threshold and r.decision == "SKIP" and r.blockers
    assert flagged.decision != "APPLY" and flagged.flags


# ------------------------------------------------------------------ what a posting asks for

@pytest.mark.parametrize("text,expected", [
    ("7 - 10 years of professional experience in frontend development", (7.0, 10.0)),  # was read as 10+
    ("with 8 to 10 years experience drive architectural decisions", (8.0, 10.0)),
    ("2 to 5 years of experience to develop responsive web apps. Minimum 2 years of experience in React", (2.0, 5.0)),
    ("Established company with 10+ years of industry presence. Frontend Developer with 2 3 years of hands-on experience",
     (2.0, 3.0)),
    ("Experience: 2 3 Years", (2.0, 3.0)),
    ("EXPERIENCE: 5+ YOE (minimum of 2–3 years in production LLM/agent systems)", (5.0, None)),
    ("serving clients for over 15 years. 3-5 years experience required", (3.0, 5.0)),
    ("Our company has over 20 years in business. Looking for 5+ years of React", (5.0, None)),
    ("10+ years experience in software engineering; TypeScript: 2+ years", (10.0, None)),  # the demanding one stays
])
def test_experience_is_read_from_what_the_posting_asks_of_you(text, expected):
    assert parse_experience(text) == expected


def test_a_junior_listing_field_does_not_skip_a_role_whose_description_asks_for_more(profile):
    """Naukri 'Senior Frontend Software Engineer': field "0-2 Yrs", description "5+ years' experience in HTML, CSS,
    JS, React" - skipped as "Role targets 0-2 years of experience; you have 7"."""
    jd = FRONTEND_JD + "\n5+ years' experience in HTML, CSS, JS, React"
    lo, hi, source, note, conflict = posting_experience(job("Senior Frontend Software Engineer", jd, "0-2 Yrs"))
    assert (lo, hi, source, conflict) == (5.0, None, "description", False) and "0-2 years" in note and "5+" in note
    r = rules(profile, "Senior Frontend Software Engineer", description=jd, experience="0-2 Yrs")
    assert r.decision == "APPLY" and any("the description is used" in x for x in r.reasons), (r.blockers, r.flags)


def test_senior_title_with_a_junior_field_and_a_silent_description_is_yours_to_decide(profile):
    """Naukri 'Senior Frontend Engineer', field "0-2 Yrs", nothing in the description: one of the two is wrong.
    Not skipped, not applied to: it waits for your decision with that sentence."""
    r = rules(profile, "Senior Frontend Engineer", experience="0-2 Yrs")
    assert r.decision == "MANUAL_REVIEW" and not r.blockers
    assert any("0-2 years" in f and "title says 'senior'" in f for f in r.flags), r.flags


@pytest.mark.parametrize("title,description,experience", [
    ("Junior Frontend Developer", FRONTEND_JD + "\n5+ years of experience", "0-2 Yrs"),  # a junior title is believed
    ("React Developer", FRONTEND_JD + "\n2 to 5 years of experience", "0 - 2 years"),  # the description agrees
    ("Frontend Developer", FRONTEND_JD, "1-2 Yrs"),  # nothing contradicts the field
])
def test_genuinely_junior_roles_are_still_skipped(profile, title, description, experience):
    r = rules(profile, title, description=description, experience=experience)
    assert r.decision == "SKIP" and any(b.startswith("Role targets") for b in r.blockers), (r.decision, r.blockers, r.flags)


# ------------------------------------------------------------------ what the local model may decide

def _says_apply(**kw):
    base = {"is_frontend_role": True, "frontend_percentage": 90, "relevance_score": 90, "important_skills": ["React"],
            "concerns": [], "recommendation": "APPLY", "confidence": 0.95, "summary": "Strong frontend fit"}
    return {**base, **kw}


MIXED_JD = "Requirements:\n- React, TypeScript, Redux\n- Python, Django, PostgreSQL, Redis, Celery\n- Docker\n" * 2  # ≈ 46% frontend


@pytest.mark.parametrize("title,description", [("Full Stack Engineer", MIXED_JD), ("Senior Software Engineer", MIXED_JD)])
async def test_the_model_cannot_promote_what_the_rules_measured(profile, title, description):
    """It answered "frontend role, apply, 0.95" for 'Forward Deployed Engineer' and for backend-heavy full-stack
    roles alike. A measured reason for review stays a reason for review."""
    p = profile.model_copy(deep=True)
    p.matching.use_ai = True
    by_rules = JobMatcher(p).evaluate_rules_only(job(title, description))
    assert by_rules.decision == "MANUAL_REVIEW", (by_rules.decision, by_rules.flags, by_rules.blockers)
    result = await JobMatcher(p, JobAssistant(fake_ollama([_says_apply()]))).evaluate(job(title, description))
    assert result.ai["used"] is True and result.decision == "MANUAL_REVIEW"
    assert "not followed" in result.ai["note"]


async def test_the_model_may_still_settle_what_the_rules_could_not_read(profile):
    p = profile.model_copy(deep=True)
    p.matching.use_ai = True
    unclear = job("Member of Technical Staff")  # an unclear title over a frontend description
    assert JobMatcher(p).evaluate_rules_only(unclear).decision == "MANUAL_REVIEW"
    result = await JobMatcher(p, JobAssistant(fake_ollama([_says_apply()]))).evaluate(unclear)
    assert result.decision == "APPLY"


# ------------------------------------------------------------------ skipped under other preferences

def _skipped(s, n, title, *, location="Hyderabad", reason="", details=None, description=FRONTEND_JD, days_old=1, **kw) -> Job:
    j = Job(portal="instahyre", external_id=f"rel-{n}", url=f"https://jobs.test/{n}", title=title, company=f"Co {n}",
            location=location, experience_text="5-8 Yrs", description=description, status=JobStatus.NEW,
            fingerprint=f"co {n}|{title.lower()}", discovered_at=utcnow() - timedelta(days=days_old), **kw)
    s.add(j)
    s.flush()
    save_match(s, j, details if details is not None else {"score": 88.0, "decision": "SKIP", "flags": [], "reasons": [],
                                                           "blockers": [reason]})
    set_status(s, j, JobStatus.SKIPPED, reason)
    return j


def test_jobs_skipped_for_their_city_are_queued_once_the_city_is_in_your_preferences(profile, clean_db):
    where = "Location 'Hyderabad' is not in your preferred locations"
    assert rules(profile, "Senior Frontend Engineer", location="Hyderabad").blockers == [where]
    with session_scope() as s:
        ids = {
            "wanted": _skipped(s, 1, "Senior Frontend Engineer", reason=where).id,
            "yours": _skipped(s, 2, "Frontend Developer", reason="Skipped by you").id,
            "rejected": _skipped(s, 3, "Frontend Engineer", reason="Rejected by you in review mode").id,
            "low": _skipped(s, 4, "Software Engineer", reason="Low match (41.0): Frontend focus ≈ 20%",
                            details={"score": 41.0, "decision": "SKIP", "blockers": [], "flags": [], "reasons": []}).id,
            "old": _skipped(s, 5, "React Developer", reason=where, days_old=90).id,
            "elsewhere": _skipped(s, 6, "React Developer", location="Kolkata",
                                  reason="Location 'Kolkata' is not in your preferred locations").id,
            "title_only": _skipped(s, 7, "UI Engineer", reason=where, description="").id,
            "excluded": _skipped(s, 8, "Java Developer", reason="Title contains excluded keyword 'java developer'").id,
        }
    with session_scope() as s:  # every one of them was decided with today's preferences
        for j in s.scalars(select(Job)):
            j.match_details = {**(j.match_details or {}), "profile_rev": preferences_rev(profile)}
    assert not {k for k, v in reconsider_skipped(profile).items() if v and k != "unchanged"}  # nothing changed: nothing moves

    p = profile.model_copy(deep=True)
    p.work_preferences.preferred_locations = [*p.work_preferences.preferred_locations, "Hyderabad"]
    assert preferences_rev(p) != preferences_rev(profile)
    moved = reconsider_skipped(p, run_id=None)
    with session_scope() as s:
        got = {name: s.get(Job, i) for name, i in ids.items()}
        status = {name: j.status for name, j in got.items()}
        assert status["wanted"] == JobStatus.QUEUED and "reconsidered after your preferences changed" in got["wanted"].status_reason
        assert "Hyderabad" in got["wanted"].status_reason  # it still says why it had been skipped
        assert got["wanted"].match_details["profile_rev"] == preferences_rev(p) and got["wanted"].decision == "APPLY"
        assert status["yours"] == status["rejected"] == JobStatus.SKIPPED  # your own decisions are never undone
        assert status["low"] == JobStatus.SKIPPED and status["old"] == JobStatus.SKIPPED
        assert status["elsewhere"] == JobStatus.SKIPPED and "Kolkata" in got["elsewhere"].status_reason
        assert status["title_only"] == JobStatus.NEW  # never read: evaluated when a search shows it again
        assert status["excluded"] == JobStatus.SKIPPED
        events = [e.message for e in s.scalars(select(ApplicationEvent).where(ApplicationEvent.type == "reconsidered"))]
        assert len(events) == 2 and any("APPLY" in m for m in events)
    assert moved["queued"] == 1 and moved["to_read_again"] == 1
    again = reconsider_skipped(p)  # stamped with these preferences: a second look changes nothing
    assert not {k for k, v in again.items() if v and k != "unchanged"}


def test_reconsidering_never_touches_submitted_or_duplicate_work(profile, clean_db):
    where = "Location 'Hyderabad' is not in your preferred locations"
    with session_scope() as s:
        sent = _skipped(s, 1, "Senior Frontend Engineer", reason=where)
        s.add(Application(job_id=sent.id, status=JobStatus.SKIPPED, submit_attempted=True))
        twin = _skipped(s, 2, "Frontend Engineer", reason=where)
        applied = Job(portal="linkedin", external_id="rel-applied", url="https://jobs.test/a", title="Frontend Engineer",
                      company="Co 2", status=JobStatus.APPLIED, fingerprint=twin.fingerprint, discovered_at=utcnow())
        s.add(applied)
        sent_id, twin_id = sent.id, twin.id
    p = profile.model_copy(deep=True)
    p.work_preferences.preferred_locations = [*p.work_preferences.preferred_locations, "Hyderabad"]
    moved = reconsider_skipped(p)
    with session_scope() as s:
        assert s.get(Job, sent_id).status == JobStatus.SKIPPED  # something may have been sent: left alone
        assert s.get(Job, twin_id).status == JobStatus.DUPLICATE  # already applied through another portal
    assert moved.get("queued", 0) == 0 and moved["duplicate"] == 1


def test_a_dry_look_changes_nothing(profile, clean_db):
    where = "Location 'Hyderabad' is not in your preferred locations"
    with session_scope() as s:
        jid = _skipped(s, 1, "Senior Frontend Engineer", reason=where).id
    p = profile.model_copy(deep=True)
    p.work_preferences.preferred_locations = [*p.work_preferences.preferred_locations, "Hyderabad"]
    assert reconsider_skipped(p, dry=True) == {"queued": 1}
    with session_scope() as s:
        j = s.get(Job, jid)
        assert j.status == JobStatus.SKIPPED and "profile_rev" not in j.match_details


# ------------------------------------------------------------------ what the portal's card says

@pytest.mark.parametrize("block,location,years", [
    ("Chennai, Mumbai\n3-7 Years", "Chennai, Mumbai", "3-7 Years"), ("Gurgaon\n4-7 Years", "Gurgaon", "4-7 Years"),
    ("Bengaluru", "Bengaluru", ""), ("Remote\n10+ years", "Remote", "10+ years"), ("", "", ""),
])
def test_instahyre_location_block_is_split_from_its_experience_line(block, location, years):
    from app.portals.instahyre import split_location

    assert split_location(block) == (location, years)


# ------------------------------------------------------------------ titles that hide what they are (queue of 2026-10-14)

@pytest.mark.parametrize("title,category,heavy", [
    (". Net Full stack developer (React JS)", "fullstack", [".NET"]),  # a space after the dot hid ".net"
    ("ASP . Net Developer", "backend", [".NET"]),
    ("Node Js Developer", "backend", []),  # its "Js" is not "JavaScript developer"
    ("Node.js Developer", "backend", []), ("NodeJS Engineer", "backend", []),
    ("React + Node Js Developer", "fullstack", []),  # a frontend technology next to Node: still yours
    ("Next.js Developer", "frontend", []), ("React JS Developer", "frontend", []), ("Vue JS Developer", "frontend", []),
    # a title that says "frontend" is a frontend role, whatever else it mentions
    ("Senior Frontend Developer (Analytics)", "frontend", []), ("Frontend Engineer - Mobile Web", "frontend", []),
    ("Front-End Developer, Data Platform UI", "frontend", []), ("Frontend Developer (Java background a plus)", "fullstack", ["Java"]),
])
def test_title_spellings_do_not_hide_the_technology(title, category, heavy):
    from app.matching.analyzer import classify_title, title_backend_stack

    assert classify_title(title) == category and title_backend_stack(title)[0] == heavy


def test_a_dot_net_role_spelled_with_a_space_is_excluded(profile):
    """'. Net Full stack developer (React JS)' was queued with a fit of 86: the excluded keyword '.net' did not match."""
    assert title_prescreen(profile, ". Net Full stack developer (React JS)") == ["Title contains excluded keyword '.net'"]
    r = rules(profile, ". Net Full stack developer (React JS)")
    assert r.decision == "SKIP" and r.blockers


@pytest.mark.parametrize("title,family", [
    ("Java or Kotlin & React.js Developer", "mobile"),  # was queued: 61.5 against an apply threshold of 60
    ("Node Js Developer", "backend"),
])
def test_a_title_that_names_another_role_family_is_never_applied_to_on_its_score(profile, title, family):
    p = profile.model_copy(deep=True)
    p.matching.apply_threshold = 60  # (as in the profile the queue came from)
    frontend_heavy = JobMatcher(p).evaluate_rules_only(job(title))
    assert frontend_heavy.decision == "MANUAL_REVIEW" and frontend_heavy.score >= 60, (frontend_heavy.score, frontend_heavy.flags)
    assert any(f.startswith(f"Title names a {family} role") for f in frontend_heavy.flags)
    mixed = JobMatcher(p).evaluate_rules_only(job(title, description=MIXED_JD))
    assert mixed.decision == "SKIP" and any(b.startswith(f"Title names a {family} role") and "your minimum is 50%" in b
                                            for b in mixed.blockers), mixed.blockers


# ------------------------------------------------------------------ level and mandatory skills (LinkedIn dry run, 2026-10-14)

def test_a_junior_role_is_not_applied_to_on_a_senior_engineers_score(profile):
    """'Junior React Developer' scored 98.6 and was dry-run applied to: everything fits but the level."""
    r = rules(profile, "Junior React Developer", experience="")
    assert r.decision == "MANUAL_REVIEW" and r.score >= profile.matching.apply_threshold
    assert any(f.startswith("Junior-level role") and "senior level" in f for f in r.flags), r.flags
    assert rules(profile, "React Developer", experience="").decision == "APPLY"  # (no level in the title: unchanged)
    p = profile.model_copy(deep=True)
    p.professional.total_experience_years = 1.5  # for a junior engineer the same posting is simply a match
    assert not any(f.startswith("Junior-level role") for f in JobMatcher(p).evaluate_rules_only(job("Junior React Developer", experience="")).flags)


MOBILE_JD = FRONTEND_JD + "\nRequirements:\n- React Native, Android and iOS releases\n- Figma hand-off\n"
JAVA_JD = FRONTEND_JD + "\nRequirements:\n- Java, Spring Boot and Hibernate services\n"


@pytest.mark.parametrize("title,description,family,named", [
    ("Staff Frontend Engineer - Mobile", MOBILE_JD, "mobile", "React Native"),
    ("React GenAI Developer", JAVA_JD, "backend", "Java"),
    ("Senior Frontend Engineer", MOBILE_JD, "mobile", "Android"),
])
def test_required_skills_of_another_platform_make_it_your_decision(profile, title, description, family, named):
    r = rules(profile, title, description=description)
    assert r.decision != "APPLY", (r.decision, r.score, r.flags)
    assert any(f"({family})" in f and named in f and "not among your skills" in f for f in r.flags) or r.blockers, r.flags


def test_small_tools_listed_as_required_do_not_hold_a_frontend_role_back(profile):
    """jQuery, Bootstrap, a test runner, a state library: required by half of all frontend postings, missing from
    most profiles, and no reason to stop."""
    jd = FRONTEND_JD + "\nRequirements:\n- jQuery, Bootstrap, Cypress, Zustand, RxJS\n- Agile ceremonies\n"
    r = rules(profile, "Senior Frontend Engineer", description=jd)
    assert r.decision == "APPLY" and not r.flags, (r.flags, r.missing_skills)


def test_mobile_web_is_web_and_mobile_is_mobile():
    from app.matching.analyzer import classify_title

    assert classify_title("Frontend Engineer - Mobile Web") == "frontend"
    assert classify_title("Mobile-first Web Developer") == "frontend"
    assert classify_title("Staff Frontend Engineer - Mobile") == "mobile"
    assert classify_title("Mobile Engineer (React Native)") == "mobile"


@pytest.mark.parametrize("heading,company,expected", [
    ("Fractal Analytics - Senior Full - Stack Engineer", "Fractal Analytics - Senior Full - Stack Engineer",
     ("Fractal Analytics", "Senior Full - Stack Engineer")),
    ("HighLevel - Lead Engineer - Fullstack (Frontend Heavy)", "", ("HighLevel", "Lead Engineer - Fullstack (Frontend Heavy)")),
    ("Bupa - Senior Frontend Developer", "Bupa", ("Bupa", "Senior Frontend Developer")),
    ("Frontend Developer", "Nova Payments", ("Nova Payments", "Frontend Developer")),  # already separate: unchanged
    ("Engineer - Frontend", "Nova Payments", ("Nova Payments", "Engineer - Frontend")),  # a dash in the role itself
    ("Frontend Developer", "", ("", "Frontend Developer")),
])
def test_instahyre_headings_are_split_into_employer_and_role(heading, company, expected):
    from app.portals.instahyre import split_company_title

    assert split_company_title(heading, company) == expected


def test_an_employers_name_no_longer_decides_what_the_role_is(profile):
    """Instahyre "Fractal Analytics - Senior Full - Stack Engineer" was skipped as "Title looks like a data role"."""
    from app.portals.instahyre import split_company_title

    assert title_prescreen(profile, "Fractal Analytics - Senior Full - Stack Engineer")  # the old title: skipped
    company, role = split_company_title("Fractal Analytics - Senior Full - Stack Engineer")
    assert company == "Fractal Analytics" and title_prescreen(profile, role) == []
