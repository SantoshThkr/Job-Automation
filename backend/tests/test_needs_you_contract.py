"""The /api/needs-you contract behind the dashboard's "Needs you" list.

Regression for the React warning "Encountered two children with the same key, `on`": the list was always one item
per job (job_id is the primary key), but a question's choices could repeat - the form scanner had stored the DOM
default value "on" as the label of radio buttons it could not read, and the dashboard keys choices by their text.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.answers.options import choice_labels
from app.db import session_scope
from app.engine.recorder import set_status, start_application, upsert_job
from app.models import AgentMode, JobStatus, QuestionAnswer
from app.portals.types import JobPosting


@pytest.mark.parametrize("options,expected", [
    (["on", "on"], []),  # two unreadable radio buttons
    (["Select an option", "Yes", "No", "Yes ", " No"], ["Yes", "No"]),
    (["India", "──────────", "──────────", "Other", "Other"], ["India", "Other"]),
    (["C++", "C#", "Go", "On-site", "ON", "On"], ["C++", "C#", "Go", "On-site", "ON", "On"]),  # real labels are kept
    (None, []), ([], []), (["", "  "], []),
])
def test_choice_labels_are_distinct_and_meaningful(options, expected):
    assert choice_labels(options) == expected


def _waiting_job(ext: str, company: str = "KitCo", title: str = "Frontend Engineer") -> tuple[int, int]:
    with session_scope() as s:
        job, _ = upsert_job(s, JobPosting(portal="linkedin", external_id=ext, url=f"https://jobs.example.com/view/{ext}",
                                          title=title, company=company, location="Noida", description="React"))
        app = start_application(s, job, AgentMode.AUTO, resume_id=None, resume_path=None)
        set_status(s, job, JobStatus.MANUAL_ACTION_REQUIRED, "Questions need your answers")
        return job.id, app.id


def _ask(app_id: int, question: str, ftype: str = "radio", options: list[str] | None = None, *, norm: str | None = None) -> None:
    with session_scope() as s:
        s.add(QuestionAnswer(application_id=app_id, question=question,
                             normalized_question=norm or question.lower().rstrip("?"), field_type=ftype,
                             options=options or [], required=True, answer="", confidence=0.0, source="none",
                             needs_review=True, note="No verified information available"))


@pytest.fixture
def api(clean_db):
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


def test_one_item_per_job_and_distinct_choices(api):
    a, app_a = _waiting_job("9001")
    b, app_b = _waiting_job("9002")  # same company and title: still its own job, its own item
    # what the scanner recorded before the fix, plus the same question asked again on a second attempt
    _ask(app_a, "Are you comfortable working from the office?", options=["on", "on"])
    _ask(app_a, "Is hybrid work fine?", options=["on", "on"])
    _ask(app_a, "Is hybrid work fine?", options=["on", "on"])  # repeated attempt: one pending question
    _ask(app_a, "Country", "select", ["Select", "India", "──────────", "──────────", "Other", "Other"])
    _ask(app_b, "Preferred shift", options=["Day", "Day", "Night"])

    data = api.get("/api/needs-you").json()
    ids = [i["job_id"] for i in data["items"]]
    assert sorted(ids) == sorted([a, b]) and len(ids) == len(set(ids)) and data["total"] == 2
    question_ids = [q["id"] for i in data["items"] for q in i["questions"]]
    assert len(question_ids) == len(set(question_ids))
    by_job = {i["job_id"]: {q["question"]: q["options"] for q in i["questions"]} for i in data["items"]}
    assert by_job[a] == {"Are you comfortable working from the office?": [], "Is hybrid work fine?": [],
                         "Country": ["India", "Other"]}
    assert by_job[b] == {"Preferred shift": ["Day", "Night"]}
    for item in data["items"]:  # what the dashboard keys by
        for q in item["questions"]:
            assert len(q["options"]) == len(set(q["options"])) and "on" not in q["options"]

    # the same contract wherever questions are listed
    pending = api.get("/api/questions/pending").json()
    assert all(len(q["options"]) == len(set(q["options"])) and "on" not in q["options"] for q in pending)
    detail = api.get(f"/api/jobs/{a}").json()
    assert all(len(q["options"]) == len(set(q["options"])) for q in detail["application"]["answers"])


def test_live_gate_questions_list_each_choice_once():
    from app.engine.questions import question_payload
    from app.browser.forms import FilledField, FormField, FormOption

    field = FormField(id="pja-1", type="radio", label="Hybrid in Bangalore?", required=True,
                      options=[FormOption("on", "pja-2"), FormOption("on", "pja-3")])
    country = FormField(id="pja-4", type="select", label="Country", required=True,
                        options=[FormOption(o, o) for o in ["Select", "India", "Other", "Other"]])
    payload = question_payload([FilledField(field, field.label, None, "unresolved"),
                                FilledField(country, country.label, None, "unresolved")])
    assert [q["options"] for q in payload] == [[], ["India", "Other"]]


@pytest.mark.browser
async def test_scanner_never_records_the_dom_default_as_a_choice(site_server, tmp_path):
    from app.browser.engine import BrowserEngine
    from app.browser.forms import scan_fields

    engine = BrowserEngine(headless=True, user_data_dir=tmp_path / "p", screenshot_dir=tmp_path / "s", human_delays=False)
    await engine.start()
    try:
        await engine.goto(f"{site_server}/experience-lab/unlabeled-radios.html")
        groups = {f.label: f.option_labels for f in await scan_fields(engine.page.locator("#f")) if f.type == "radio"}
    finally:
        await engine.stop()
    assert groups["Are you comfortable working from the office?"] == ["", ""]  # unreadable - and not "on", "on"
    assert groups["Preferred shift"] == ["day", "night"]  # an explicit value attribute still stands in
    assert groups["Open to relocation?"] == ["Yes", "No"]
    assert choice_labels(groups["Are you comfortable working from the office?"]) == []
