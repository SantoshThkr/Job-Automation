"""Central engine units behind the cross-portal 'Needs you' fixes (no browser): derived facts the runs asked for,
skill-list semantics, work-experience entries, reuse scopes of your answers, element/form classification, and the
'Needs you' API (exact problem, missing info, suggestions, answer -> continue, park / retry)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.answers.answerer import Answerer, memory_scope, skill_years_key
from app.answers.question_bank import QuestionContext, format_date_for, skill_groups
from app.db import session_scope
from app.engine.needs_you import classify_stop
from app.engine.recorder import memory_lookup_fn, remember_answer
from app.matching.analyzer import JobInput
from app.models import AgentState, AnswerMemory, Application, Job, JobStatus, QuestionAnswer
from app.navigator.classify import Kind, classify_element

JOB = JobInput(title="Frontend Engineer", company="KitCo", location="Bengaluru", description="React TypeScript")


async def ans(profile, question, ftype="text", options=(), *, section=None, memory=None, job=JOB, portal="site"):
    ctx = QuestionContext(question=question, field_type=ftype, options=list(options), required=True, job=job,
                          portal=portal, section=section or {})
    return await Answerer(profile, None, memory_lookup=memory).answer(ctx)


# ---------------------------------------------------------------- facts the runs asked you for

@pytest.mark.parametrize("question,ftype,options,expected", [
    ("Please select your total additional months of experience:", "select", ["Select", *map(str, range(12))], "0"),
    ("Experience", "text", [], "7"),  # crashed the classifier before (max() of an empty sequence)
    ("Experience (whole years)", "text", [], "7"),
    ("Are you located in Bangalore?", "text", [], "Yes"),  # Bengaluru == Bangalore
    ("Are you located in Pune?", "radio", ["Yes", "No"], "No"),
    ("are you fresher?", "radio", ["Yes", "No"], "No"),
    ("Are you a fresher or experienced?", "select", ["Fresher", "Experienced"], "Experienced"),
    ("What is your desired annual base compensation?", "text", [], "3800000"),
    ("What is your expected pay?", "text", [], "38 LPA"),
    ("By using this form you agree with the storage and handling of your data", "checkbox", [], "Yes"),
    ("Job title (current)", "text", [], "Senior Frontend Engineer"),
])
async def test_derivable_facts(profile, question, ftype, options, expected):
    a = await ans(profile, question, ftype, options)
    assert a.value is not None and a.value.startswith(expected) and not a.needs_review, (a.value, a.note)


async def test_last_working_date_in_the_format_the_label_asks(profile):
    profile.professional.last_working_day = "5 March 2026"
    assert (await ans(profile, "Last Working Date (DD/MM/YY)")).value == "05/03/26"
    assert (await ans(profile, "Last working date (MM/DD/YYYY)")).value == "03/05/2026"
    assert (await ans(profile, "Last working day", "date")).value == "2026-03-05"
    from datetime import date

    assert format_date_for("relieving date (yyyy-mm-dd)", date(2026, 3, 5)) == "2026-03-05"


async def test_salary_for_a_job_abroad_is_not_guessed(profile):
    a = await ans(profile, "What is your expected pay?", job=JobInput(title="FE", company="GitLab",
                                                                      location="Remote, United States"))
    assert a.value is None and "outside India" in a.note


async def test_previous_employment_names_the_right_company(profile):
    a = await ans(profile, "Have you previously worked at or consulted for GitLab?")
    assert a.value is None and "'gitlab' is not in your work history" in a.note  # asked, never assumed 'No'


# ---------------------------------------------------------------- skill lists: 'and' needs all, '/' 'or' any

def test_skill_groups():
    assert skill_groups("years designing or working with rest apis, microservices, and api integrations") == [
        (["Microservices", "REST APIs"], False)]
    groups = skill_groups("full stack technologies such as react/angular/vue.js and java/spring boot/node.js/python")
    assert [any_of for _, any_of in groups] == [True, True] and "React" in groups[0][0]


@pytest.mark.parametrize("question,expected,review", [
    ("How many years of experience do you have with React or Angular?", "6", False),  # React 6 (any of)
    ("How many years of experience do you have with React and Node.js?", "2", False),  # both: the lower
    ("How many years of experience do you have designing or working with REST APIs, microservices, and API "
     "integrations?", "0", True),  # microservices is not in your profile: never claimed from REST APIs
    ("How many years of experience do you have with Cloud platforms such as AWS, Azure, or GCP?", "0", True),
])
async def test_skill_years_never_overclaim(profile, question, expected, review):
    a = await ans(profile, question)
    assert (a.value or "0") == expected and a.needs_review == review, (a.value, a.note)


# ---------------------------------------------------------------- one entry of a work-experience list

async def test_role_description_comes_from_that_roles_work_history(profile):
    a = await ans(profile, "Description*", "textarea", section={"title": "Frontend Developer", "company": "Pixel Labs"})
    assert a.value == "Built SPAs in React and Redux. Technologies: React, Redux, JavaScript."
    assert a.sources == ["work_history: Pixel Labs"]


async def test_title_inside_a_past_role_is_that_roles_title(profile):
    past = await ans(profile, "Title*", section={"company": "Pixel Labs Pvt. Ltd."})
    assert past.value == "Frontend Developer"
    unknown = await ans(profile, "Description", "textarea", section={"company": "Some Other Corp"})
    assert unknown.value is None and "not in your work history" in unknown.note  # nothing invented


async def test_description_without_an_entry_is_not_a_role_description(profile):
    a = await ans(profile, "Description", "textarea")
    assert a.mode in {"ask", "composed", "ai_generated"} and "Pixel Labs" not in (a.value or "")


# ---------------------------------------------------------------- where your answers are reused

def test_memory_scopes():
    assert memory_scope("UNKNOWN", "What is your blood group?", "KitCo") == ("global", "")
    assert memory_scope("WHY_COMPANY", "Why do you want to join KitCo?", "KitCo") == ("company", "kitco")
    assert memory_scope("UNKNOWN", "Where did you find this job on Naukri?", "KitCo", "naukri") == ("portal", "naukri")
    assert skill_years_key("Years of experience in MongoDB") == "skill years: mongodb"
    assert skill_years_key("Experience with React and Redux (years)") is None  # one skill only


async def test_skill_years_answer_is_reused_for_every_wording(clean_db, profile):
    with session_scope() as s:
        remember_answer(s, "MongoDB experience (years)", "2")
    a = await ans(profile, "How many years of work experience do you have with MongoDB?", memory=memory_lookup_fn())
    assert a.value == "2" and a.source == "memory"
    m = await ans(profile, "How many months of experience do you have with MongoDB?", memory=memory_lookup_fn())
    assert m.value == "24"


async def test_company_and_portal_answers_stay_there(clean_db, profile):
    with session_scope() as s:
        remember_answer(s, "Why do you want to join KitCo?", "Because of the payments product.", company="KitCo")
        remember_answer(s, "What is your Naukri profile ID?", "asha-v-123", portal="naukri")
    other = JobInput(title="FE", company="OtherCo", location="Bengaluru")
    assert (await ans(profile, "Why do you want to join KitCo?", "textarea", memory=memory_lookup_fn(),
                      job=other)).source != "memory"
    assert (await ans(profile, "What is your Naukri profile ID?", memory=memory_lookup_fn(),
                      portal="linkedin")).source != "memory"
    assert (await ans(profile, "What is your Naukri profile ID?", memory=memory_lookup_fn(),
                      portal="naukri")).value == "asha-v-123"


# ---------------------------------------------------------------- what the navigator may click

@pytest.mark.parametrize("text,kind", [
    ("Power up with up to 2x recruiter actions Become a Pro", Kind.DANGER), ("Try Premium for ₹0", Kind.DANGER),
    ("Know your rights", Kind.DANGER), ("Privacy policy", Kind.DANGER), ("Send message", Kind.DANGER),
    ("Let's talk", Kind.DANGER), ("Request a demo", Kind.DANGER), ("Subscribe", Kind.DANGER),
])
def test_never_clicked(text, kind):
    assert classify_element(text, form_context=True) == kind


@pytest.mark.parametrize("text", ["Search", "Find jobs", "Apply filters", "Show results"])
def test_search_buttons_never_submit(text):
    assert classify_element(text, type_="submit", form_context=True) == Kind.OTHER


@pytest.mark.parametrize("reason,kind", [
    ("Required question(s) need your answer: Description", "questions"),
    ("hire.fabrichq.ai asks you to sign in or create an account", "account"),
    ("Please solve the CAPTCHA / security check on ventriks.io", "verification"),
    ("neoscript.in takes applications by email: send your resume to hello@neoscript.in", "email"),
    ("chalo.com returned an error page ('502 Bad Gateway') right after submitting", "site_error"),
    ("Naukri did not register the Apply click (after reloading, the job still shows 'Apply') - nothing was sent",
     "not_registered"),
    ("Submitted but no confirmation was detected - please verify on kemsys.com", "unconfirmed"),
    ("On nissicomputing.com the assistant could not tell how to continue", "navigation"),
])
def test_stop_reasons_are_classified(reason, kind):
    assert classify_stop(reason)[0] == kind


# ---------------------------------------------------------------- the 'Needs you' API

def _parked_job(company="KitCo", portal="naukri") -> int:
    with session_scope() as s:
        job = Job(portal=portal, external_id=f"ny-{company}", url=f"https://example.test/{company}", title="Frontend Engineer",
                  company=company, location="Bengaluru", fingerprint=f"fp-{company}", status=JobStatus.MANUAL_ACTION_REQUIRED,
                  status_reason="Required question(s) need your answer: What is your blood group?; Work Authorization")
        s.add(job)
        s.flush()
        app = Application(job_id=job.id, status=JobStatus.MANUAL_ACTION_REQUIRED, mode="auto", apply_target="external",
                          manual_action_reason=job.status_reason, last_step="Filling an application form on kitco.test")
        s.add(app)
        s.flush()
        for q, ftype, opts, note in [
            ("What is your blood group?", "text", [], "No verified information available"),
            ("Work Authorization", "select", ["Select", "Citizen", "Permanent Resident"],
             "the form asks for your status (Citizen, Permanent Resident); ..."),
            ("How many years of work experience do you have with Kubernetes?", "text", [],
             "'Kubernetes' is not in your skills; confirm 0 years"),
            ("Job Title", "text", [], "a message, not a question"),  # what run 8 recorded; the profile knows it now
        ]:
            s.add(QuestionAnswer(application_id=app.id, question=q, normalized_question=q.lower().rstrip("?"),
                                 field_type=ftype, options=opts, required=True, answer="", confidence=0.0,
                                 source="none", needs_review=True, note=note))
        return job.id


@pytest.fixture
def api(clean_db):
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


def test_needs_you_item_explains_everything(api):
    job_id = _parked_job()
    data = api.get("/api/needs-you").json()
    item = next(i for i in data["items"] if i["job_id"] == job_id)
    assert item["stop_kind"] == "questions" and item["company"] == "KitCo" and item["portal"] == "naukri"
    assert item["step"].startswith("Filling an application form") and item["resume"] == "restart"
    qs = {q["question"]: q for q in item["questions"]}
    assert qs["Job Title"]["profile_can_answer"] and qs["Job Title"]["suggestion"] == "Senior Frontend Engineer"
    wa = qs["Work Authorization"]
    assert wa["sensitive"] and wa["suggestion"] is None and "citizenship" in wa["missing"]  # never suggested
    assert qs["How many years of work experience do you have with Kubernetes?"]["suggestion"] == "0"
    assert qs["What is your blood group?"]["saved_answer"] is None and item["unanswered"] == 3


def test_answering_requeues_the_job_and_scopes_the_answers(api):
    job_id = _parked_job()
    other_id = _parked_job(company="OtherCo")
    r = api.post(f"/api/needs-you/{job_id}/answers", json={"answers": [
        {"question": "What is your blood group?", "answer": "O+", "scope": "auto"},
        {"question": "Work Authorization", "answer": "Citizen", "scope": "company"},
        {"question": "How many years of work experience do you have with Kubernetes?", "answer": "0", "scope": "auto"},
    ]}).json()
    assert r["requeued"] and r["status"] == "QUEUED" and not r["live"]
    assert "Retry" in r["resume_note"] or "starts this application again" in r["resume_note"]
    with session_scope() as s:
        wa = s.scalar(select(AnswerMemory).where(AnswerMemory.question == "Work Authorization"))
        assert (wa.scope, wa.company) == ("company", "kitco")
        assert s.get(Job, other_id).status == JobStatus.MANUAL_ACTION_REQUIRED  # KitCo's answer is not OtherCo's


def test_answer_goes_into_the_open_form_when_the_agent_is_waiting(api):
    job_id = _parked_job()
    with session_scope() as s:
        st = AgentState.get(s)
        st.gate_kind, st.pending_job_id = "questions", job_id
        st.gate_payload = {"questions": [{"id": "pja-7", "question": "What is your blood group?"}]}
    r = api.post(f"/api/needs-you/{job_id}/answers", json={"answers": [
        {"question": "What is your blood group?", "answer": "O+"}]}).json()
    assert r["live"] and not r["requeued"] and r["resume"] == "live"
    with session_scope() as s:
        st = AgentState.get(s)
        assert st.gate_response == "approve" and st.gate_answers == {"pja-7": "O+"}


def test_park_and_retry(api):
    job_id = _parked_job()
    assert api.post(f"/api/jobs/{job_id}/action", json={"action": "park"}).json()["status"] == "PARKED"
    assert all(i["job_id"] != job_id for i in api.get("/api/needs-you").json()["items"])
    data = api.get("/api/needs-you?include_parked=true").json()
    assert data["parked"] == 1 and any(i["job_id"] == job_id for i in data["items"])
    assert api.post(f"/api/jobs/{job_id}/action", json={"action": "retry"}).json()["status"] == "QUEUED"
