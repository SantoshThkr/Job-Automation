"""Saved answers: asked once, reused with the right scope, correctable - and never written by a guess.

Scopes: global (every form), portal, company, job. Each scope keeps its own answer for a question; the most specific
one that applies is used. A general answer is also kept under a wording-independent key when the question has one,
so "D.O.B" answered once also answers "Date of birth (DD/MM/YYYY)".
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text

from app.answers.answerer import Answerer, MemoryHit
from app.answers.question_bank import QuestionContext, normalize_question
from app.db import session_scope
from app.engine.needs_you import saved_answer
from app.engine.recorder import (forget_memory, lookup_memory, memory_lookup_fn, remember_answer, set_status,
                                 start_application, update_memory, upsert_job)
from app.matching.analyzer import JobInput
from app.models import AgentMode, AnswerMemory, JobStatus, QuestionAnswer
from app.portals.types import JobPosting

Q = "What is your date of birth?"
ACME = JobInput(title="Frontend Engineer", company="Acme Labs Pvt Ltd", location="Pune")
ACME_OTHER_JOB = JobInput(title="Platform Engineer", company="Acme Labs", location="Pune")
OTHER = JobInput(title="Frontend Engineer", company="Borealis Systems", location="Pune")


def ctx(question, job=ACME, portal="naukri", field_type="text", options=None):
    return QuestionContext(question=question, field_type=field_type, options=options or [], required=True, job=job,
                           portal=portal)


@pytest.fixture
def engine(profile, clean_db):
    return Answerer(profile, None, memory_lookup=memory_lookup_fn(), min_confidence=0.75)


def rows() -> list[tuple[str, str, str, str, str, str]]:
    with session_scope() as s:
        return sorted((r.normalized_question, r.scope, r.company, r.portal, r.job_key, r.answer)
                      for r in s.scalars(select(AnswerMemory)))


# ------------------------------------------------------------------ scopes and precedence

async def test_each_scope_keeps_its_own_answer_and_the_most_specific_wins(engine):
    question = "What is your expected joining bonus?"
    with session_scope() as s:
        remember_answer(s, question, "general", scope="global")
        remember_answer(s, question, "on naukri", scope="portal", portal="naukri")
        remember_answer(s, question, "for acme", scope="company", company="Acme Labs")
        remember_answer(s, question, "for this job", scope="job", company="Acme Labs", title="Frontend Engineer")
    assert len([r for r in rows() if r[0] == normalize_question(question)]) == 4  # nothing replaced anything

    async def answer(job, portal):
        a = await engine.answer(ctx(question, job, portal))
        return a.value, a.mode

    assert await answer(ACME, "naukri") == ("for this job", "memory")
    assert await answer(ACME_OTHER_JOB, "naukri") == ("for acme", "memory")  # same employer, another job
    assert await answer(OTHER, "naukri") == ("on naukri", "memory")
    assert await answer(OTHER, "linkedin") == ("general", "memory")


async def test_an_answer_for_one_employer_does_not_replace_or_leak_your_general_one(engine):
    question = "Which design tool do you use most?"
    with session_scope() as s:
        remember_answer(s, question, "Figma", scope="global")
        remember_answer(s, question, "Sketch", scope="company", company="Acme Labs", portal="naukri")
    assert (await engine.answer(ctx(question, ACME))).value == "Sketch"
    assert (await engine.answer(ctx(question, OTHER))).value == "Figma"


async def test_a_job_only_answer_is_not_reused_for_any_other_job(engine):
    question = "Which of our products would you like to work on?"
    with session_scope() as s:
        remember_answer(s, question, "Payments", scope="job", company="Acme Labs", title="Frontend Engineer")
    assert (await engine.answer(ctx(question, ACME))).value == "Payments"
    for job in (ACME_OTHER_JOB, OTHER):
        a = await engine.answer(ctx(question, job))
        assert a.value is None and a.mode == "ask", (job.title, a.value)


def test_automatic_scope(clean_db):
    with session_scope() as s:
        why = remember_answer(s, "Why do you want to work at Acme Labs?", "x", company="Acme Labs", portal="naukri")
        found = remember_answer(s, "How did you find this job on Naukri?", "search", company="Acme Labs", portal="naukri")
        dated = remember_answer(s, "Are you available for an interview on 17th Sep?", "Yes", company="Acme Labs")
        fact = remember_answer(s, "What is your hometown?", "Mysuru", company="Acme Labs", portal="naukri")
        assert (why.scope, why.company) == ("company", "acme labs")
        assert (found.scope, found.portal) == ("portal", "naukri")
        assert dated.scope == "company"
        assert (fact.scope, fact.company, fact.portal) == ("global", "", "")


def test_a_scope_that_has_nothing_to_tie_it_to_falls_back(clean_db):
    with session_scope() as s:
        assert remember_answer(s, "Q one for nobody?", "a", scope="company").scope == "global"
        assert remember_answer(s, "Q two for nobody?", "a", scope="job", company="Acme Labs").scope == "company"
        assert remember_answer(s, "Q three for nobody?", "a", scope="portal").scope == "global"


# ------------------------------------------------------------------ one answer for every wording

async def test_a_fact_answered_once_answers_every_wording(engine):
    assert (await engine.answer(ctx("Date of Birth (DD/MM/YYYY)"))).mode == "ask"
    with session_scope() as s:
        remember_answer(s, "D.O.B", "01/01/1990", company="Acme Labs", portal="naukri")
    for wording in ("Date of Birth (DD/MM/YYYY)", "What is your birth date?", "DOB"):
        a = await engine.answer(ctx(wording, OTHER, "linkedin"))
        assert (a.value, a.mode) == ("01/01/1990", "memory"), (wording, a.value, a.note)
    assert (await engine.answer(ctx("Hometown City"))).mode == "ask"  # another fact is still asked


async def test_reason_for_change_is_your_words_not_a_generated_text(engine):
    with session_scope() as s:
        remember_answer(s, "Reason for change", "Looking for a product company with a larger frontend scope.")
    a = await engine.answer(ctx("Why are you looking for a job change?", OTHER, field_type="textarea"))
    assert a.mode == "memory" and a.value.startswith("Looking for a product company")


async def test_years_of_a_discipline_answered_once_fit_any_control(engine):
    with session_scope() as s:
        remember_answer(s, "How many years of work experience do you have as Full Stack Developer?", "5")
    typed = await engine.answer(ctx("How many years of full stack development experience do you have?", OTHER))
    assert (typed.value, typed.mode) == ("5", "memory")
    ranged = await engine.answer(ctx(
        "How many years of professional experience do you have as a full-stack engineer, owning features end to end?",
        OTHER, field_type="radio", options=["Less than 3 years", "3-4 years", "5-7 years", "8+ years"]))
    assert (ranged.value, ranged.mode) == ("5-7 years", "memory")
    total = await engine.answer(ctx("Total experience"))
    assert total.value == "7" and total.mode == "profile"  # your total is still the profile fact


async def test_skill_years_in_months_and_other_wordings(engine):
    with session_scope() as s:
        remember_answer(s, "How many years of experience do you have in Kubernetes?", "1.5")
    assert (await engine.answer(ctx("How long have you used Kubernetes in production?"))).value == "1.5"
    assert (await engine.answer(ctx("Kubernetes experience (in months)"))).value == "18"


# ------------------------------------------------------------------ conflicts

async def test_the_profile_wins_over_a_saved_answer(engine):
    with session_scope() as s:
        remember_answer(s, "Current city", "Mumbai")
    a = await engine.answer(ctx("Current city"))
    assert (a.value, a.mode) == ("Bengaluru", "profile")


async def test_your_own_words_come_before_anything_written_for_you(engine):
    question = "Why do you want to join us?"
    with session_scope() as s:
        remember_answer(s, question, "I have followed your design system work for years.", company="Acme Labs")
    mine = await engine.answer(ctx(question, ACME, field_type="textarea"))
    assert mine.mode == "memory" and mine.value.startswith("I have followed")
    other = await engine.answer(ctx(question, OTHER, field_type="textarea"))
    assert other.mode != "memory" and "design system work" not in (other.value or "")  # never reused for another employer


async def test_answering_the_same_question_again_replaces_only_that_scope(engine):
    question = "What is your hometown?"
    with session_scope() as s:
        remember_answer(s, question, "Mysuru")
        remember_answer(s, question, "Mysore", scope="company", company="Acme Labs")
        remember_answer(s, question, "Mysuru, Karnataka")  # the general answer, given again
    assert [r[5] for r in rows() if r[0] == normalize_question(question)] == ["Mysore", "Mysuru, Karnataka"] or \
        sorted(r[5] for r in rows() if r[0] == normalize_question(question)) == ["Mysore", "Mysuru, Karnataka"]
    assert (await engine.answer(ctx("Native place", OTHER))).value == "Mysuru, Karnataka"


# ------------------------------------------------------------------ correcting and deleting

async def test_a_corrected_answer_is_what_every_later_form_gets(engine):
    with session_scope() as s:
        remember_answer(s, "D.O.B", "01/01/1990")
        row = s.scalar(select(AnswerMemory).where(AnswerMemory.normalized_question == "d.o.b"))
        update_memory(s, row, "02/02/1990")
    for wording in ("D.O.B", "Date of birth"):
        assert (await engine.answer(ctx(wording))).value == "02/02/1990"


async def test_correcting_years_updates_the_wording_independent_answer(engine):
    with session_scope() as s:
        row = remember_answer(s, "How many years of experience do you have in Kubernetes?", "1")
        update_memory(s, row, "2 years")
    assert (await engine.answer(ctx("Kubernetes (years)"))).value == "2"


async def test_deleting_a_saved_answer_removes_every_trace_of_it(engine):
    with session_scope() as s:
        row = remember_answer(s, "D.O.B", "01/01/1990")
        forget_memory(s, row)
    assert rows() == []
    assert (await engine.answer(ctx("Date of birth"))).mode == "ask"


def test_deleting_one_scope_keeps_the_others(clean_db):
    with session_scope() as s:
        remember_answer(s, "What is your hometown?", "Mysuru")
        scoped = remember_answer(s, "What is your hometown?", "Mysore", scope="company", company="Acme Labs")
        forget_memory(s, scoped)
    with session_scope() as s:
        assert saved_answer(s, "Hometown", company="Acme Labs").answer == "Mysuru"


# ------------------------------------------------------------------ nothing is saved by a guess

async def test_answers_the_engine_produces_are_never_saved(profile, clean_db):
    from app.ai.job_assistant import JobAssistant
    from tests.conftest import fake_ollama

    provider = fake_ollama([{"category": "CUSTOM", "confidence": 0.9},
                            {"answer": "Visual Studio Code", "confidence": 0.95, "profile_fields_used": ["skills"]}] * 3)
    engine = Answerer(profile, JobAssistant(provider), memory_lookup=memory_lookup_fn(), min_confidence=0.75)
    for question in ("Which code editor do you use?", "Current city", "Why do you want to join us?", "D.O.B"):
        await engine.answer(ctx(question))
    assert rows() == []  # only remember_answer writes, and only your own answers reach it


# ------------------------------------------------------------------ older lookups keep working

async def test_a_lookup_that_returns_plain_text_or_one_hit_still_works(profile):
    plain = Answerer(profile, None, memory_lookup=lambda nq: "Mysuru" if "hometown" in nq else None, min_confidence=0.75)
    assert (await plain.answer(ctx("What is your hometown?"))).value == "Mysuru"
    scoped = Answerer(profile, None, min_confidence=0.75, memory_lookup=lambda nq: [
        MemoryHit("general"), MemoryHit("for acme", scope="company", company="acme labs")] if "bonus" in nq else None)
    assert (await scoped.answer(ctx("Expected joining bonus?", ACME))).value == "for acme"
    assert (await scoped.answer(ctx("Expected joining bonus?", OTHER))).value == "general"


def test_lookup_without_a_job_returns_every_scope(clean_db):
    with session_scope() as s:
        remember_answer(s, "What is your expected joining bonus?", "general")
        remember_answer(s, "What is your expected joining bonus?", "for acme", scope="company", company="Acme Labs")
        hits = lookup_memory(s, normalize_question("What is your expected joining bonus?"))
        assert sorted(h.scope for h in hits) == ["company", "global"]
        one = lookup_memory(s, normalize_question("What is your expected joining bonus?"), company="Acme Labs")
        assert one.answer == "for acme"
        assert lookup_memory(s, normalize_question("What is your expected joining bonus?"), company="Nobody").answer == "general"


# ------------------------------------------------------------------ the database written by earlier versions

LEGACY_DDL = """CREATE TABLE answer_memory (
    id INTEGER NOT NULL, normalized_question TEXT NOT NULL, question TEXT NOT NULL, answer TEXT NOT NULL,
    times_used INTEGER NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL,
    "category" VARCHAR(32) DEFAULT '', "scope" VARCHAR(16) DEFAULT 'global', "company" VARCHAR(255) DEFAULT '',
    "portal" VARCHAR(32) DEFAULT '', PRIMARY KEY (id), UNIQUE (normalized_question))"""


def test_an_older_database_is_upgraded_without_losing_a_saved_answer(tmp_path):
    from app.db import _migrate_answer_memory_scopes

    path = tmp_path / "jobs.db"
    con = sqlite3.connect(path)
    con.execute(LEGACY_DDL)
    con.executemany("INSERT INTO answer_memory (normalized_question, question, answer, times_used, created_at, updated_at, "
                    "category, scope, company, portal) VALUES (?,?,?,?,?,?,?,?,?,?)", [
        ("what is your hometown", "What is your hometown?", "Mysuru", 3, "2026-10-01 10:00:00", "2026-10-02 10:00:00",
         "SENSITIVE_ID", "global", "", ""),
        ("why acme labs", "Why Acme Labs?", "x", 0, "2026-10-01 10:00:00", "2026-10-01 10:00:00", "WHY_COMPANY",
         "company", "acme labs", ""),
        ("legacy row without scope", "Legacy row without scope", "y", 1, "2026-10-01 10:00:00", "2026-10-01 10:00:00",
         None, None, None, None),
    ])
    con.commit()
    con.close()

    engine = create_engine(f"sqlite:///{path}")
    _migrate_answer_memory_scopes(engine)
    _migrate_answer_memory_scopes(engine)  # running it again changes nothing

    insp = inspect(engine)
    assert not any(u["column_names"] == ["normalized_question"] for u in insp.get_unique_constraints("answer_memory"))
    assert "job_key" in {c["name"] for c in insp.get_columns("answer_memory")}
    assert not insp.has_table("answer_memory_legacy")
    with engine.connect() as conn:
        kept = conn.execute(text("SELECT normalized_question, answer, times_used, scope, company, portal, job_key, category "
                                 "FROM answer_memory ORDER BY id")).all()
        assert kept == [("what is your hometown", "Mysuru", 3, "global", "", "", "", "SENSITIVE_ID"),
                        ("why acme labs", "x", 0, "company", "acme labs", "", "", "WHY_COMPANY"),
                        ("legacy row without scope", "y", 1, "global", "", "", "", "")]
        # the point of the upgrade: an employer's answer now sits next to the general one
        conn.execute(text("INSERT INTO answer_memory (normalized_question, question, answer, times_used, created_at, "
                          "updated_at, category, scope, company, portal, job_key) VALUES ('what is your hometown', 'q', "
                          "'Mysore', 0, '2026-10-12', '2026-10-12', '', 'company', 'acme labs', '', '')"))
        with pytest.raises(Exception, match="UNIQUE"):  # one answer per question and scope
            conn.execute(text("INSERT INTO answer_memory (normalized_question, question, answer, times_used, created_at, "
                              "updated_at, category, scope, company, portal, job_key) VALUES ('what is your hometown', "
                              "'q', 'again', 0, '2026-10-12', '2026-10-12', '', 'company', 'acme labs', '', '')"))
    backups = list(tmp_path.glob("jobs.before-saved-answer-scopes-*.db"))
    assert len(backups) == 1 and oct(backups[0].stat().st_mode & 0o777) == "0o600"
    copy = sqlite3.connect(backups[0])
    assert copy.execute("SELECT COUNT(*) FROM answer_memory").fetchone() == (3,)  # the copy is the database as it was
    copy.close()
    engine.dispose()


def test_a_current_database_is_left_alone(tmp_path, clean_db):
    from app.db import _migrate_answer_memory_scopes, get_engine

    engine = get_engine()
    with session_scope() as s:
        remember_answer(s, "What is your hometown?", "Mysuru")
    _migrate_answer_memory_scopes(engine)
    assert [r[5] for r in rows() if r[1] == "global"] == ["Mysuru"]
    from pathlib import Path

    assert not list(Path(engine.url.database).parent.glob("*.before-saved-answer-scopes-*"))


# ------------------------------------------------------------------ the dashboard's API

@pytest.fixture
def api(clean_db):
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


def _waiting_job(ext: str, company: str, title: str, question: str) -> int:
    with session_scope() as s:
        job, _ = upsert_job(s, JobPosting(portal="linkedin", external_id=ext, url=f"https://jobs.example.com/view/{ext}",
                                          title=title, company=company, location="Pune", description="React"))
        app = start_application(s, job, AgentMode.AUTO, resume_id=None, resume_path=None)
        set_status(s, job, JobStatus.MANUAL_ACTION_REQUIRED, "Questions need your answers")
        s.add(QuestionAnswer(application_id=app.id, question=question, normalized_question=normalize_question(question),
                             field_type="text", options=[], required=True, answer="", confidence=0.0, source="none",
                             needs_review=True, note="No verified information available"))
        return job.id


def test_answering_for_this_job_only_does_not_release_other_jobs(api):
    question = "Which of our teams interests you most?"
    a = _waiting_job("7001", "Acme Labs", "Frontend Engineer", question)
    b = _waiting_job("7002", "Acme Labs", "Platform Engineer", question)
    c = _waiting_job("7003", "Borealis Systems", "Frontend Engineer", question)
    r = api.post(f"/api/needs-you/{a}/answers", json={"answers": [{"question": question, "answer": "Design systems", "scope": "job"}]})
    assert r.status_code == 200 and r.json()["saved"][0]["scope"] == "job" and r.json()["saved"][0]["job"] == "frontend engineer"
    items = {i["job_id"]: i for i in api.get("/api/needs-you").json()["items"]}
    assert a not in items  # answered: back in the queue
    assert items[b]["questions"][0]["saved_answer"] is None and items[c]["questions"][0]["saved_answer"] is None
    assert {q["job_id"] for q in api.get("/api/questions/pending").json()} == {c} or \
        {q["job_id"] for q in api.get("/api/questions/pending").json()} == {b}  # one row per distinct question


def test_scope_must_be_a_known_one(api):
    a = _waiting_job("7010", "Acme Labs", "Frontend Engineer", "Hometown?")
    r = api.post(f"/api/needs-you/{a}/answers", json={"answers": [{"question": "Hometown?", "answer": "x", "scope": "planet"}]})
    assert r.status_code == 400 and "scope" in r.json()["detail"]


def test_a_saved_answer_can_be_corrected_through_the_api(api):
    question = "What is your date of birth?"
    job = _waiting_job("7020", "Acme Labs", "Frontend Engineer", "D.O.B")
    saved = api.post("/api/answers/memory", json={"question": question, "answer": "01/01/1990"}).json()
    assert saved["scope"] == "global"
    fixed = api.put(f"/api/answers/memory/{saved['id']}", json={"answer": " 02/02/1990 "})
    assert fixed.status_code == 200 and fixed.json()["answer"] == "02/02/1990" and fixed.json()["scope"] == "global"
    with session_scope() as s:
        assert saved_answer(s, "Date of birth (DD/MM/YYYY)", company="Anyone").answer == "02/02/1990"
        assert saved_answer(s, "D.O.B", company="Acme Labs").answer == "02/02/1990"
    listed = {m["question"]: m for m in api.get("/api/answers/memory").json() if m["scope"] != "category"}
    assert listed[question]["answer"] == "02/02/1990"
    assert job not in {i["job_id"] for i in api.get("/api/needs-you").json()["items"]}  # nothing else was blocking it


@pytest.mark.parametrize("body,status", [
    ({"answer": ""}, 422), ({"answer": "   "}, 400), ({}, 422), ({"answer": "x", "scope": "global"}, 422),
    ({"answer": "x" * 5001}, 422),
])
def test_correction_rejects_bad_input(api, body, status):
    saved = api.post("/api/answers/memory", json={"question": "What is your hometown?", "answer": "Mysuru"}).json()
    assert api.put(f"/api/answers/memory/{saved['id']}", json=body).status_code == status
    assert api.get("/api/answers/memory").json()[0]["answer"] in {"Mysuru"}


def test_correcting_or_deleting_an_unknown_answer_is_404(api):
    assert api.put("/api/answers/memory/999999", json={"answer": "x"}).status_code == 404
    assert api.delete("/api/answers/memory/999999").status_code == 404
    assert api.put("/api/answers/memory/0", json={"answer": "x"}).status_code == 422


def test_deleting_through_the_api_removes_the_wording_independent_answer_too(api):
    saved = api.post("/api/answers/memory", json={"question": "D.O.B", "answer": "01/01/1990"}).json()
    assert len(api.get("/api/answers/memory").json()) == 2  # the answer and its key
    assert api.delete(f"/api/answers/memory/{saved['id']}").json() == {"ok": True}
    assert api.get("/api/answers/memory").json() == []
