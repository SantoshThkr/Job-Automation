"""Goal 1 - submission verification (engine/submission.py) and Goal 2 - experience values fitted to the field
(answers/normalize.py + browser/forms.py). Mock pages: tests/fixtures/sites/submit-lab, experience-lab."""

from __future__ import annotations

import pytest

from app.answers.normalize import fit_number, refit_after_rejection
from app.answers.options import choose_option
from app.ats.external import apply_external
from app.browser.engine import BrowserEngine
from app.engine.submission import PageState, SubmissionState, decide
from app.models import AgentMode
from app.portals.types import ApplyOutcome, JobPosting
from tests.helpers import FakeGate, make_ctx


# ================================================================ Goal 1: the verdict (pure)

def _msg(text, region="page", kinds=("success",), desc=False):
    return {"text": text, "kinds": list(kinds), "region": region, "long": False, "inDescription": desc}


BEFORE = PageState(url="https://co.test/apply", fields=["name", "email"], submit_visible=True, submit_enabled=True,
                   messages=[_msg("Application submitted candidates are reviewed every Friday", "page", desc=True)])


@pytest.mark.parametrize("after,state", [
    (PageState(url=BEFORE.url, fields=[], messages=[_msg("Application submitted", "dialog")]), "CONFIRMED"),
    (PageState(url=BEFORE.url, fields=[], messages=[_msg("Your application has been received.", "alert")]), "CONFIRMED"),
    (PageState(url=BEFORE.url, fields=[], messages=[_msg("Reference number: APP-2026-1234", kinds=("reference",))]),
     "CONFIRMED"),
    (PageState(url="https://co.test/thank-you.php", fields=[], messages=[_msg("Thank You!", "heading", ("thanks",))]),
     "CONFIRMED"),  # Speckyfox: confirmation URL + a new "Thank You!" heading + the form is gone
    (PageState(url=BEFORE.url, fields=["name", "email"], submit_visible=True, submit_enabled=True,
               messages=[_msg("Failed to submit application")], errors=[]), "FAILED"),  # Onesofts
    (PageState(url=BEFORE.url, fields=["name", "email"], submit_visible=True, errors=["Please enter a valid phone number."]),
     "REFUSED"),
])
def test_verdicts(after, state):
    for m in after.messages:
        if "failure" not in m["kinds"] and "Failed" in m["text"]:
            m["kinds"] = ["failure"]
    assert decide(BEFORE, after, final=True).state.value == state


def test_job_description_text_never_confirms():
    """'Application submitted ...' was already in the job description: no confirmation, however it is placed."""
    after = PageState(url=BEFORE.url, fields=["name", "email"], submit_visible=True, submit_enabled=True,
                      messages=list(BEFORE.messages))
    assert decide(BEFORE, after, final=True).state != SubmissionState.CONFIRMED
    jd_after_navigation = PageState(url="https://co.test/jobs/2", fields=[], messages=[
        _msg("Application submitted candidates are reviewed every Friday", "heading", desc=True)])
    assert decide(BEFORE, jd_after_navigation, final=True).state != SubmissionState.CONFIRMED


def test_unrelated_page_text_and_url_change_alone_never_confirm():
    widget = PageState(url=BEFORE.url, fields=["name", "email"], submit_visible=True, submit_enabled=True,
                       messages=[*BEFORE.messages, _msg("Application submitted by 12 candidates today")])
    assert decide(BEFORE, widget, final=True).state == SubmissionState.UNCERTAIN
    moved = PageState(url="https://co.test/careers", fields=[])  # navigated, form gone, nothing said
    v = decide(BEFORE, moved, final=True)
    assert v.state == SubmissionState.UNCERTAIN and "form_gone" in " ".join(v.evidence)


def test_a_sign_in_page_after_the_submit_never_confirms():
    """intellohire (run #41): after "Save" the site went to its login page, whose own start-up call was answered
    {"success": true}. Neither that answer nor the vanished form says the application was taken."""
    from app.engine.submission import NetworkHit

    login = PageState(url="https://co.test/", title="Login", fields=["phone", "terms"],
                      body_start="Go to website Login / Signup +91 (IND) I accept all terms & conditions Get OTP")
    sent = NetworkHit("POST", "https://co.test/public/apply/candidate-save", 200)
    backend = NetworkHit("POST", "https://pass.backend.test/", 200, success=True, later_page=True)
    v = decide(BEFORE, login, network=[sent, backend], final=True, elapsed=5)
    assert v.state == SubmissionState.UNCERTAIN, v
    assert "sign-in page" in v.message and "candidate-save -> 200" in v.message
    assert "sign_in_page" in " ".join(v.evidence) and "submit_request_accepted" not in " ".join(v.evidence)
    assert decide(BEFORE, login, network=[sent, backend], final=False, elapsed=5).state == \
        SubmissionState.WAITING_FOR_SUBMISSION_RESULT  # (never taken as the next step of the application either)
    # the same answer to the form's own request still counts, and a thank-you page is still a confirmation
    own = NetworkHit("POST", "https://co.test/api/apply", 200, success=True)
    gone = PageState(url=BEFORE.url, fields=[])
    assert decide(BEFORE, gone, network=[own], final=True).state == SubmissionState.CONFIRMED
    assert decide(BEFORE, gone, network=[backend], final=True).state == SubmissionState.UNCERTAIN
    thanks = PageState(url="https://co.test/thank-you", title="Thank you", fields=[],
                       messages=[_msg("Thank You!", "heading", ("thanks",))])
    assert decide(BEFORE, thanks, network=[backend], final=True).state == SubmissionState.CONFIRMED


# ================================================================ Goal 1: real pages

@pytest.fixture
async def engine(site_server, tmp_path):
    e = BrowserEngine(headless=True, user_data_dir=tmp_path / "p", screenshot_dir=tmp_path / "s", human_delays=False)
    await e.start()
    yield e
    await e.stop()


JOB = JobPosting(portal="naukri", external_id="lab-1", url="https://labco.test/jobs/1", title="Frontend Engineer",
                 company="LabCo", location="Noida", description="React TypeScript")


async def _submit_lab(engine, site_server, profile, mode):
    await engine.goto(f"{site_server}/submit-lab/form.html?mode={mode}")
    ctx = make_ctx(engine, profile, JOB, AgentMode.AUTO, FakeGate())
    res = await apply_external(engine, engine.page, ctx)
    submits = await engine.page.evaluate("() => window.SUBMITS") if not engine.page.is_closed() else None
    return res, ctx, submits


@pytest.mark.browser
@pytest.mark.parametrize("mode,confirmation", [
    ("thankyou", "Thank You!"),  # explicit thank-you page after a redirect (Speckyfox)
    ("redirect", "Application complete"),  # redirect to a confirmation route
    ("modal", "Application submitted"),  # success modal
    ("message", "Your application has been received."),  # confirmation message, same page
    ("samepage", "Thanks for applying!"),  # the form is replaced by the confirmation
    ("reference", "Reference number: APP-2026-1234"),  # application / reference number
    ("network", "Submission acknowledged by the site"),  # the ATS's JSON answer: success + application id
    ("gotit", "Thanks \u2014 we've got it."),  # yobitel.com (run #16): plain paragraphs + "Reference: YA-01591"
    ("rsc", "Submission acknowledged by the site"),  # a Next.js server action's reply: ok + reference
    ("popup", "Your application for React.js Developer has been received."),  # vikisol.in (run #11): custom modal
    ("slow", "Your application has been submitted successfully."),  # the server answers after 29 s (> the 25 s wait)
])
async def test_confirmed_submissions(engine, site_server, profile, mode, confirmation):
    res, ctx, _ = await _submit_lab(engine, site_server, profile, mode)
    assert res.outcome == ApplyOutcome.APPLIED and res.verification == "confirmed", (res.message, res.evidence)
    assert confirmation in res.confirmation_text and res.evidence
    assert len(ctx.submits) == 1


@pytest.mark.browser
@pytest.mark.parametrize("mode,why", [
    ("none", "submit_request_sent: POST"),  # a request went out, nothing readable came back
    ("reset", "confirmation not verified"),  # Brisk Mind: the form was simply cleared
    ("unrelated", "confirmation not verified"),  # unrelated text mentioning 'application submitted'
    ("servererror", "returned an error page ('502 Bad Gateway')"),
    ("rscspam", "submit_request_accepted"),  # ok:true but no reference: one signal is not a confirmation
    ("jobref", "confirmation not verified"),  # another job's page with its own "Reference: JR-12345"
])
async def test_uncertain_submissions_are_needs_you_never_applied(engine, site_server, profile, mode, why):
    res, ctx, submits = await _submit_lab(engine, site_server, profile, mode)
    assert res.outcome == ApplyOutcome.MANUAL_ACTION_REQUIRED and res.verification == "uncertain", res.message
    assert why in res.message and res.submit_attempted
    assert len(ctx.submits) == 1 and submits in (1, None)  # never submitted twice


@pytest.mark.browser
async def test_failed_submission_is_failed_and_not_resubmitted(engine, site_server, profile):
    res, ctx, submits = await _submit_lab(engine, site_server, profile, "failed")
    assert res.outcome == ApplyOutcome.FAILED and res.verification == "failed"
    assert "Failed to submit application" in res.message and submits == 1 and len(ctx.submits) == 1


@pytest.mark.browser
async def test_lost_click_is_clicked_once_more(engine, site_server, profile):
    res, ctx, submits = await _submit_lab(engine, site_server, profile, "lostclick")
    assert res.outcome == ApplyOutcome.APPLIED and submits == 1
    assert any(t == "submit_retry" for t, _ in ctx.events)


@pytest.mark.browser
async def test_silent_refusal_is_not_submitted_three_times(engine, site_server, profile):
    """Sunrays (run #10): the form stayed as it was and was submitted three times. Now: at most one more click."""
    res, ctx, _ = await _submit_lab(engine, site_server, profile, "sticky")
    assert res.outcome == ApplyOutcome.MANUAL_ACTION_REQUIRED and res.verification == "uncertain"
    assert len(ctx.submits) <= 2


# ---------------------------------------------------------------- retry verification / mark as applied

def _uncertain_job(site_server, final_url: str) -> int:
    from app.db import session_scope
    from app.models import Application, Job, JobStatus

    with session_scope() as s:
        job = Job(portal="naukri", external_id=f"v-{abs(hash(final_url))}", url=f"{site_server}/naukri/job-listings-x-1",
                  title="Frontend Engineer", company="LabCo", location="Noida", fingerprint=f"fp-{final_url}",
                  status=JobStatus.MANUAL_ACTION_REQUIRED, status_reason="Submitted - confirmation not verified")
        s.add(job)
        s.flush()
        s.add(Application(job_id=job.id, status=JobStatus.MANUAL_ACTION_REQUIRED, mode="auto", apply_target="external",
                          submit_attempted=True, final_url=final_url, verification_state="uncertain",
                          manual_action_reason="Submitted - confirmation not verified on labco"))
        return job.id


@pytest.mark.browser
@pytest.mark.parametrize("page,expected", [("thank-you.html", "APPLIED"), ("form.html?mode=none", "MANUAL_ACTION_REQUIRED")])
async def test_retry_verification_never_resubmits(site_server, tmp_path, clean_db, page, expected):
    from fastapi.testclient import TestClient

    from app.db import session_scope
    from app.main import create_app
    from app.models import Job
    from tests.test_central_engine_cross_portal import _run_naukri

    job_id = _uncertain_job(site_server, f"{site_server}/submit-lab/{page}")
    with TestClient(create_app()) as c:
        assert c.post(f"/api/jobs/{job_id}/action", json={"action": "verify"}).json()["status"] == "QUEUED"
        item_kind = None
    engine = BrowserEngine(headless=True, user_data_dir=tmp_path / "p", screenshot_dir=tmp_path / "s", human_delays=False)
    try:
        summary = await _run_naukri(engine, site_server)
    finally:
        await engine.stop()
    with session_scope() as s:
        job = s.get(Job, job_id)
        assert job.status == expected, job.status_reason
        assert job.application.verify_requested is False and job.application.verification_checked_at is not None
        if expected == "APPLIED":
            assert job.application.confirmation_text.startswith("Verified later") and job.application.verification_state == "confirmed"
        else:
            assert "re-checked" in job.status_reason and job.application.verification_state == "uncertain"
    assert summary["stats"].get("apply_attempts", 0) == 0  # a verification is not an application
    assert item_kind is None


def test_unverified_submission_in_needs_you_and_mark_as_applied(clean_db, site_server):
    from fastapi.testclient import TestClient

    from app.main import create_app

    job_id = _uncertain_job(site_server, f"{site_server}/submit-lab/form.html")
    with TestClient(create_app()) as c:
        item = next(i for i in c.get("/api/needs-you").json()["items"] if i["job_id"] == job_id)
        assert item["stop_kind"] == "unconfirmed" and item["resume"] == "verify_first"
        assert item["verification"] == "uncertain"
        r = c.post(f"/api/jobs/{job_id}/action", json={"action": "mark_applied", "note": "Checked on the site"})
        assert r.json()["status"] == "APPLIED"


# ================================================================ Goal 2: experience values

@pytest.mark.parametrize("raw,dom,expected", [
    ("6.5", {"field_type": "number", "step": "any"}, "6.5"),  # decimal input
    ("6.5", {"field_type": "number", "step": "0.5"}, "6.5"),
    ("6.5", {"field_type": "number"}, "6"),  # HTML default step 1: integers only -> 6, never 7
    ("6.5", {"field_type": "text", "pattern": "[0-9]+"}, "6"),
    ("6.5", {"field_type": "text", "inputmode": "numeric"}, "6"),
    ("6.5", {"field_type": "text", "hint": "Total experience (in whole years)"}, "6"),
    ("6.5", {"field_type": "text"}, "6.5"),  # nothing says otherwise: the canonical value
    ("6.5", {"field_type": "number", "step": "1", "max_": "5"}, None),  # cannot be entered truthfully
    ("6.9", {"field_type": "number"}, "6"),  # floored, not rounded
])
def test_fit_number(raw, dom, expected):
    assert fit_number(raw, **dom).value == expected


def test_refit_after_a_rejection_reads_the_message_once():
    assert refit_after_rejection("6.5", "Enter a whole number between 0 and 99", field_type="text").value == "6"
    unknown = refit_after_rejection("6.5", "Enter the value as YYMM", field_type="text")
    assert unknown.value is None and "YYMM" in unknown.note  # no guessing: it is left for you


@pytest.mark.parametrize("options,expected", [
    ([str(i) for i in range(11)], "6"), (["0-2 years", "3-5 years", "6-8 years", "9+ years"], "6-8 years"),
    (["5 years", "6 years", "7 years"], "6 years"), (["6 years", "6.5 years", "7 years"], "6.5 years"),
    (["1-3", "4-6", "7-9"], "4-6"), (["1", "2", "3"], "3"), (["7", "8", "9"], None),
])
def test_dropdowns_never_overstate(options, expected):
    assert choose_option("6.5", ["Select", *options], numeric=6.5) == expected


@pytest.mark.parametrize("options,expected", [
    (["Fresher", "Intermediate", "1+ Experience", "5+ Experience"], "5+ Experience"),  # markschamp.com (run #16)
    (["0+ years", "2+ years", "5+ years", "10+ years"], "5+ years"),
])
def test_open_ranges_pick_the_highest_floor_not_the_first(options, expected):
    assert choose_option("6.5", options, numeric=6.5) == expected


def test_fresher_is_never_chosen_for_years_of_experience():
    """invecent.in (run #11): 'Experience: Experienced / Fresher' was answered 'Fresher' for 7 years - the 'highest
    option below the value' fallback took the zero-experience option. Fresher is untrue, not an understatement."""
    assert choose_option("7", ["Select Experience", "Experienced", "Fresher"], numeric=7.0) == "Experienced"
    assert choose_option("0", ["Experienced", "Fresher"], numeric=0.0) == "Fresher"
    assert choose_option("7", ["Fresher", "0-1 years", "1-3 years", "3-5 years", "5+ years"], numeric=7.0) == "5+ years"
    assert choose_option("7", ["Fresher", "Intern"], numeric=7.0) is None  # nothing true to pick: left for you


def test_do_you_have_experience_on_an_unreadable_dropdown_is_yes_no(profile):
    """greenhouse (run #11): 'Do you have recent hands-on experience working with JavaScript/TypeScript?' on a
    dropdown whose options could not be read was answered '5.5'."""
    from app.answers.question_bank import QuestionContext, resolve

    ans = resolve(QuestionContext(question="Do you have recent hands-on experience working with JavaScript?",
                                  field_type="combobox", options=[]), profile)
    assert ans is not None and ans.value == "Yes"


def test_reference_and_acknowledgement_wording():
    import re

    from app.engine.submission import _JSON_APP_ID, GENERIC_THANKS, REFERENCE_SHOWN

    shown = re.compile(REFERENCE_SHOWN, re.I)
    assert shown.search("Reference: YA-01591") and shown.search("Ref #: 884213")
    assert not shown.search("Job Reference: JR-12345") and not shown.search("Reference: see the attached brochure")
    thanks = re.compile(GENERIC_THANKS, re.I)
    for t in ["Thanks \u2014 we've got it.", "Thank you!", "A recruiter will review your application within 7 days.",
              "We've got your application", "Our HR team or a relevant team member will contact you within 48 hours."]:
        assert thanks.search(t), t
    assert _JSON_APP_ID.search('{"applicationId": "APP-2026-0042"}') and _JSON_APP_ID.search('{"candidate_id": 88213}')
    for body in ['{"applicationId": null}', '{"reference": "spam"}', '{"reference": "submitted"}']:
        assert not _JSON_APP_ID.search(body), body


def test_an_unlabeled_dropdown_of_experience_levels_is_overall_experience(profile):
    from app.answers.question_bank import QuestionContext, resolve

    ans = resolve(QuestionContext(question="example Form Control S", field_type="select",
                                  options=["Fresher", "Intermediate", "1+ Experience", "5+ Experience"]), profile)
    assert ans is not None and ans.numeric == profile.professional.total_experience_years
    # not for other numeric dropdowns ("Age": no experience wording in the options)
    assert resolve(QuestionContext(question="example Form Control A", field_type="select",
                                   options=["18-25 years", "26-35 years", "36+ years"]), profile) is None


@pytest.mark.browser
async def test_browser_default_dropdowns_are_never_submitted_as_answers(engine, site_server, profile):
    """markschamp.com (run #16) sent 'Fresher' and 'Android Developer' for a Frontend role: the two unlabeled
    <select>s still showed their first option and were kept as 'prefilled'. Now an unchosen first option is no
    answer: experience levels are answered from the profile, anything else is left for you - never the default."""
    from app.browser.forms import FormFiller, scan_fields

    ctx = make_ctx(engine, profile, JOB, AgentMode.AUTO, FakeGate())
    await engine.goto(f"{site_server}/experience-lab/modal.html")
    page = engine.page
    fields = {f.id: f for f in await scan_fields(page.locator("#m"))}
    by_dom = {await page.locator(f.selector).get_attribute("id"): f for f in fields.values()}
    assert by_dom["exampleFormControlS"].defaulted and by_dom["exampleFormControlS"].is_empty
    assert by_dom["exampleFormControlSelect1"].defaulted and by_dom["exampleFormControlSelect1"].required
    assert not by_dom["cn"].defaulted and by_dom["cn"].value == "India"  # chosen by the page: kept as before
    assert not by_dom["src"].defaulted and not by_dom["src"].required  # a "Select" placeholder: still optional
    assert all(f.label.strip() not in {"\u00d7", "x"} for f in fields.values())  # the close button is no label

    filler: FormFiller = ctx.form_filler()
    report = await filler.fill(page.locator("#m"))
    assert await page.locator("#exampleFormControlS").evaluate("e => e.value") == "5+ Experience"
    position = next(i for i in report.items if i.field.selector == by_dom["exampleFormControlSelect1"].selector)
    assert position.status != "prefilled"  # never kept as 'Android Developer' without anyone choosing it
    if position.status in {"unresolved", "failed"}:
        assert position in report.unresolved  # -> Needs You with this question
    # a second pass over the same modal sees the chosen experience as answered (no refill loop)
    again = {await page.locator(f.selector).get_attribute("id"): f for f in await scan_fields(page.locator("#m"))}
    assert not again["exampleFormControlS"].defaulted and again["exampleFormControlS"].value == "5+ Experience"


@pytest.mark.browser
async def test_experience_fields_follow_the_dom(engine, site_server, profile):
    """React 6.5, React Hooks (= React), Next.js 3.5, TypeScript 4.5: entered as each field accepts them; the
    profile keeps 6.5; a whole-number rule shown only after Next is read and fixed once; an unknown format is
    left for you."""
    from app.browser.forms import FormFiller

    profile.skills.years.update({"React": 6.5, "Next.js": 3.5, "TypeScript": 4.5})
    ctx = make_ctx(engine, profile, JOB, AgentMode.AUTO, FakeGate())
    await engine.goto(f"{site_server}/experience-lab/form.html")
    page = engine.page
    filler: FormFiller = ctx.form_filler()
    form = page.locator("#exp")
    report = await filler.fill(form)
    values = {k: await page.locator(f"#{k}").input_value() for k in ["e1", "e2", "e3", "e4", "e5", "e6", "e7", "e8", "e9"]}
    assert values == {"e1": "6", "e2": "6.5", "e3": "3", "e4": "4.5", "e5": "6", "e6": "6-8 years", "e7": "6 years",
                      "e8": "6.5 years", "e9": "6.5"}
    await page.click("#next")
    assert await page.evaluate("() => window.NEXT_OK") is False
    fixed = await filler.fix_invalid(form)
    assert await page.locator("#e9").input_value() == "6" and any("e9" in f or "React" in f for f in fixed.fixed)
    assert any("YYMM" in m for m in fixed.messages)  # still invalid: reported for you, not guessed
    assert profile.skills.years["React"] == 6.5  # the canonical value is untouched
    assert not any(i.field.id and i.status == "failed" for i in report.items if i.field.label.startswith("Years"))



class RealisticGate:
    """Like the dashboard gate: reports RESOLVED as soon as its 'done' check passes, else times out."""

    def __init__(self):
        self.calls = []
        self.last_answers = {}

    async def wait(self, kind, reason, *, page=None, detect_done=None, timeout_s=None, payload=None):
        from app.engine.context import GateResult

        self.calls.append(kind)
        if detect_done is not None and await detect_done():
            return GateResult.RESOLVED
        return GateResult.TIMEOUT


@pytest.mark.browser
async def test_captcha_after_submit_never_loops(engine, site_server, profile):
    """Run #15: careerpage.co looped 852 times on 'please complete the CAPTCHA' (resolved at once, asked again)."""
    import asyncio

    await engine.goto(f"{site_server}/submit-lab/form.html?mode=captchapoll")
    gate = RealisticGate()
    ctx = make_ctx(engine, profile, JOB, AgentMode.AUTO, gate, auto_wait=30)
    res = await asyncio.wait_for(apply_external(engine, engine.page, ctx), timeout=240)
    assert res.outcome == ApplyOutcome.MANUAL_ACTION_REQUIRED and "CAPTCHA" in res.message
    assert gate.calls.count("captcha") <= 2 and len(ctx.submits) == 1


class _UnconfirmedPortal:
    """A portal adapter whose own submit ended without a readable result (LinkedIn, run #13: the tab closed while
    waiting) - and whose job page, read again, shows the job as applied (or not)."""
    name, display_name = "linkedin", "LinkedIn"

    def __init__(self, applied_now: bool):
        self.applied_now, self.reads, self.applies = applied_now, 0, 0

    async def apply(self, posting, ctx):
        self.applies += 1
        await ctx.mark_submit_attempt()
        from app.portals.types import ApplyResult

        return ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED, "Submitted - confirmation not verified: the page was "
                           "closed before a result", submit_attempted=True, verification="uncertain")

    async def fetch_details(self, posting):
        self.reads += 1
        posting.already_applied = self.applied_now
        return posting


@pytest.mark.browser
@pytest.mark.parametrize("applied_now", [True, False])
async def test_an_unverified_portal_submit_is_read_back_on_the_portal_once(site_server, tmp_path, clean_db, applied_now):
    from app.db import session_scope
    from app.engine.control import AgentControl
    from app.engine.orchestrator import Orchestrator
    from app.engine.recorder import set_status, upsert_job
    from app.models import Job, JobStatus

    with session_scope() as s:
        job, _ = upsert_job(s, JobPosting(portal="linkedin", external_id="4400000001", url=f"{site_server}/linkedin-new/jobs/view/4400000001/",
                                          title="Frontend Engineer", company="Kokoro Lab", description="React"))
        set_status(s, job, JobStatus.QUEUED, "Relevant")
        job_id = job.id
    engine = BrowserEngine(headless=True, user_data_dir=tmp_path / "p", screenshot_dir=tmp_path / "s", human_delays=False)
    await engine.start()
    adapter = _UnconfirmedPortal(applied_now)
    try:
        orch = Orchestrator(engine, AgentControl(cache_s=0))
        orch.mode = AgentMode.AUTO
        res = await orch.apply_job(job_id, adapter, JobPosting(portal="linkedin", external_id="4400000001",
                                                               url=f"{site_server}/linkedin-new/jobs/view/4400000001/",
                                                               title="Frontend Engineer", company="Kokoro Lab"))
    finally:
        await engine.stop()
    assert adapter.applies == 1 and adapter.reads == 1  # read once, never submitted again
    with session_scope() as s:
        job = s.get(Job, job_id)
        if applied_now:
            assert res.outcome == ApplyOutcome.APPLIED and job.status == JobStatus.APPLIED
            assert any("portal_status: LinkedIn shows this job as applied" in e for e in job.application.verification_evidence)
        else:
            assert job.status == JobStatus.MANUAL_ACTION_REQUIRED and job.application.verification_state == "uncertain"
            assert any("does not show the job as applied" in e for e in res.evidence)


@pytest.mark.parametrize("raw,message,dom,expected", [
    ("6.5", "Invalid input", {}, "6"),  # LinkedIn (run #18): React 6.5 rejected with no format hint -> the whole number
    ("6.5", "Please enter a valid answer", {}, "6"),
    ("6.5", "Invalid input", {"step": "any"}, None),  # decimals allowed: the rejection is about something else
    ("6", "Invalid input", {}, None),  # already whole: nothing else to try - left for you
    ("6.5", "Must be between 7 and 20", {}, None),  # out of range: never changed to fit
])
def test_a_generic_rejection_of_a_decimal_tries_the_whole_number_once(raw, message, dom, expected):
    assert refit_after_rejection(raw, message, **dom).value == expected


@pytest.mark.browser
async def test_radio_groups_that_re_render_on_selection_are_set_and_verified(engine, site_server, profile):
    """LinkedIn Easy Apply (run #18): 'could not set value Yes' on 3 of 20 applications - the click worked, but the
    group re-rendered and the clicked <input> no longer existed, so its state could not be read back."""
    from app.browser.forms import FormFiller, scan_fields

    ctx = make_ctx(engine, profile, JOB, AgentMode.AUTO, FakeGate())
    await engine.goto(f"{site_server}/experience-lab/radio-rerender.html")
    page = engine.page
    dialog = page.locator("#dlg")
    filler: FormFiller = ctx.form_filler()
    groups = [f for f in await scan_fields(dialog) if f.type == "radio"]
    assert [f.option_labels for f in groups] == [["Yes", "No"], ["Yes", "No"]]
    for f, value in zip(groups, ["Yes", "No"]):
        loc = dialog.locator(f.selector)
        assert await filler.set_value(loc, f, value, root=dialog)
        assert await filler.verify(loc, f, value, root=dialog) == (True, "")
    checked = await page.evaluate("() => Array.from(document.querySelectorAll('[role=radio][aria-checked=true] p')).map(p => p.innerText)")
    assert checked == ["Yes", "No"]


@pytest.mark.browser
async def test_a_script_refusal_in_an_error_label_is_fixed_and_resubmitted(engine, site_server, profile):
    """flipkoins.com (run #21): jQuery Validate refused the form ('um...yea, you have to write something to send this
    form.' in <label class="error">) - it was reported as 'submitted, confirmation not verified' although nothing was
    sent. The refusal is read, the Message box filled, the form sent once."""
    await engine.goto(f"{site_server}/submit-lab/jqvalidate.html")
    ctx = make_ctx(engine, profile, JOB, AgentMode.AUTO, FakeGate())
    res = await apply_external(engine, engine.page, ctx)
    assert res.outcome == ApplyOutcome.APPLIED and "Applied successfully!" in res.confirmation_text, (res.message, res.evidence)
    assert await engine.page.evaluate("() => window.SUBMITS") == 1
