"""Real-site cases behind avoidable 'Needs you' items, replayed on local pages (synthetic copies, no real data).

Confirmation: what a site says after a genuine application is read correctly (Contact Form 7, a Drupal webform's
confirmation route, a general thank-you page) - and a sales enquiry form is never sent as an application.
Forms: a value the site calls too long is shortened once, a per-digit code is a verification step (yours), and a
'select all that apply' list is ticked from the profile.
"""

from __future__ import annotations

import pytest

from app.ats.external import apply_external
from app.browser.engine import BrowserEngine
from app.browser.forms import collect_errors, scan_fields
from app.engine.submission import _ANALYTICS, _CAPTCHA_NET, NetworkHit, PageState, SubmissionState, decide
from app.engine.context import GateResult
from app.models import AgentMode
from app.navigator.classify import CONTACT_BUTTON_RX, Kind, classify_element
from app.portals.types import ApplyOutcome, JobPosting
from tests.helpers import FakeGate, make_ctx

pytestmark = pytest.mark.browser

JOB = JobPosting(portal="naukri", external_id="lab-9", url="https://labco.test/jobs/9", title="React JS Developer",
                 company="LabCo", location="Pune", description="React TypeScript Redux")


@pytest.fixture
async def engine(site_server, tmp_path):
    e = BrowserEngine(headless=True, user_data_dir=tmp_path / "p", screenshot_dir=tmp_path / "s", human_delays=False)
    await e.start()
    yield e
    await e.stop()


async def _apply(engine, profile, url, mode=AgentMode.AUTO):
    await engine.goto(url)
    ctx = make_ctx(engine, profile, JOB, mode, FakeGate())
    res = await apply_external(engine, engine.page, ctx)
    submits = await engine.page.evaluate("() => window.SUBMITS") if not engine.page.is_closed() else None
    return res, ctx, submits


# ------------------------------------------------------------------ confirmation after a genuine application

@pytest.mark.parametrize("mode,confirmation", [
    ("cf7", "Thank you for your message. It has been sent."),  # Contact Form 7: {"status": "mail_sent"} + its message
    ("drupal", "New submission added to Job Application."),  # a webform's /confirmation route
    ("reachout", "Thank you for reaching out!"),  # the site's general thank-you page after the careers form
])
async def test_site_acknowledgements_of_an_application_are_confirmed(engine, site_server, profile, mode, confirmation):
    res, ctx, submits = await _apply(engine, profile, f"{site_server}/submit-lab/form.html?mode={mode}")
    assert res.outcome == ApplyOutcome.APPLIED and res.verification == "confirmed", (res.message, res.evidence)
    assert confirmation in res.confirmation_text
    assert len({e.split(":")[0] for e in res.evidence}) >= 2  # never one signal alone
    assert len(ctx.submits) == 1 and submits in (1, None)


async def test_a_message_acknowledgement_without_a_resume_is_never_an_application(engine, site_server, profile):
    """nauticalinfosolutions.com (run #26): "Thank you for your inquiry!" after a form that took no resume was recorded
    as applied. Here the button says "Submit application", so the form is sent - and its contact-form
    acknowledgement still confirms nothing."""
    res, ctx, submits = await _apply(engine, profile, f"{site_server}/submit-lab/form.html?mode=cf7bare")
    assert res.outcome == ApplyOutcome.MANUAL_ACTION_REQUIRED and res.verification == "uncertain", (res.outcome, res.message)
    assert "acknowledged a message, not an application" in res.message and res.submit_attempted
    assert len(ctx.submits) == 1 and submits == 1  # sent once, never again


async def test_contact_form_7_spam_refusal_is_a_failure_not_an_application(engine, site_server, profile):
    res, ctx, submits = await _apply(engine, profile, f"{site_server}/submit-lab/form.html?mode=cf7spam")
    assert res.outcome == ApplyOutcome.FAILED and res.verification == "failed", (res.outcome, res.message)
    assert "error trying to send your message" in res.message and submits == 1 and len(ctx.submits) == 1


async def test_an_accepted_request_alone_is_still_not_a_confirmation(engine, site_server, profile):
    res, ctx, _ = await _apply(engine, profile, f"{site_server}/submit-lab/form.html?mode=cf7quiet")
    assert res.outcome == ApplyOutcome.MANUAL_ACTION_REQUIRED and res.verification == "uncertain", res.message
    assert res.submit_attempted and len(ctx.submits) == 1


# ------------------------------------------------------------------ a sales / enquiry form is not an application

@pytest.mark.parametrize("page,said", [
    ("index.html", "contact"),  # "INQUIRE NOW": recognised as the site's enquiry form, never filled
    ("plain.html", "contact / enquiry form"),  # "Tell us about your project" + a bare "Submit": the sales form
    ("bare.html", "contact / enquiry form"),  # the company's home page: nothing here is about applying
    ("careers-contact.html", "contact / enquiry form"),  # a "Careers" page whose only form is the contact widget
    ("sales-lead.html", "contact"),  # the agency's lead form ("Company Name") on its careers page
    ("budget.html", "contact"),  # a quote form ("Budget") under "Apply for this job": never filled, nothing asked
])
async def test_an_enquiry_form_on_a_job_page_is_never_submitted(engine, site_server, profile, page, said):
    res, ctx, submits = await _apply(engine, profile, f"{site_server}/careers/inquiry/{page}")
    assert res.outcome == ApplyOutcome.MANUAL_ACTION_REQUIRED, (res.outcome, res.message)
    assert said in res.message.lower() and not res.submit_attempted
    assert submits == 0 and ctx.submits == []  # your details went nowhere
    assert res.verification != "confirmed"
    if page == "budget.html":
        assert await engine.page.input_value("#n") == "" and not ctx.gate.calls  # not filled, and no "Budget?" for you


async def test_the_same_boxes_with_a_resume_upload_are_an_application(engine, site_server, profile):
    res, ctx, submits = await _apply(engine, profile, f"{site_server}/careers/inquiry/apply.html")
    assert res.outcome == ApplyOutcome.APPLIED and res.verification == "confirmed", (res.message, res.evidence)
    assert "Thank you for your message. It has been sent." in res.confirmation_text and submits == 1


@pytest.mark.parametrize("label,contact", [
    ("INQUIRE NOW", True), ("Enquire Now", True), ("Get a Free Quote", True), ("Request a Callback", True),
    ("Hire Developers", True), ("Start your project", True),
    ("Apply Now", False), ("Submit Application", False), ("Submit", False), ("Send", False), ("Hiring now - apply", False),
])
def test_enquiry_buttons(label, contact):
    assert bool(CONTACT_BUTTON_RX.search(label.lower())) is contact
    if contact:
        assert classify_element(label, form_context=True) == Kind.DANGER  # never clicked


# ------------------------------------------------------------------ requests that are not the submission

@pytest.mark.parametrize("url,ignored", [
    ("https://www.google.com/pagead/form-data/808772993", True), ("https://www.google.com/rmkt/collect/808772993", True),
    ("https://www.google.co.in/pagead/1p-conversion/1/", True), ("https://api-js.datadome.co/js/", True),
    ("https://www.googleadservices.com/pagead/conversion/1/", True),
    ("https://example.test/wp-json/contact-form-7/v1/contact-forms/890/feedback", False),
    ("https://example.test/api/job-form/", False), ("https://boards.example.test/applications", False),
])
def test_advertising_and_bot_detection_requests_are_not_submissions(url, ignored):
    assert bool(_ANALYTICS.search(url)) is ignored
    assert not _CAPTCHA_NET.search(url)  # ...and telemetry is not a CAPTCHA challenge either


def test_contact_form_7_statuses():
    before = PageState(url="https://co.test/careers", fields=["name", "email"], submit_visible=True, submit_enabled=True)
    shown = dict(text="Thank you for your message. It has been sent.", kinds=["thanks"], region="page")
    sent = NetworkHit("POST", "https://co.test/wp-json/contact-form-7/v1/contact-forms/1/feedback", 200, success=True)
    after = PageState(url=before.url, fields=["name", "email"], submit_visible=True, submit_enabled=True, messages=[shown])
    assert decide(before, after, network=[sent], final=True).state == SubmissionState.CONFIRMED
    assert decide(before, after, network=[], final=True).state == SubmissionState.UNCERTAIN  # words alone
    quiet = PageState(url=before.url, fields=["name", "email"], submit_visible=True, submit_enabled=True)
    assert decide(before, quiet, network=[sent], final=True).state == SubmissionState.UNCERTAIN  # request alone
    refused = NetworkHit("POST", sent.url, 200, failure=True, message="There was an error trying to send your message.")
    verdict = decide(before, quiet, network=[refused], final=True)
    assert verdict.state == SubmissionState.FAILED and "There was an error trying to send your message." in verdict.message


# ------------------------------------------------------------------ form handling

async def test_a_value_the_site_calls_too_long_is_shortened_and_sent_once(engine, site_server, profile):
    res, ctx, submits = await _apply(engine, profile, f"{site_server}/submit-lab/toolong.html")
    assert res.outcome == ApplyOutcome.APPLIED, (res.outcome, res.message)
    sent = await engine.page.evaluate("() => window.SENT")
    assert 0 < len(sent) <= 200 and submits == 1
    assert await engine.page.evaluate("() => window.REFUSALS") == 1  # refused once, repaired, never looped
    assert not sent.endswith(("  ", ","))


async def test_a_code_typed_digit_by_digit_is_a_verification_step_not_six_questions(engine, site_server):
    await engine.goto(f"{site_server}/engine/otp-digits.html")
    assert "otp" in await engine.detect_blockers()
    assert "otp" in await engine.detect_blockers(include_widgets=False)
    await engine.goto(f"{site_server}/engine/form.html")
    assert "otp" not in await engine.detect_blockers()  # an ordinary form (phone, postal code...) is not one


async def test_select_all_that_apply_is_ticked_from_the_profile(engine, site_server, profile):
    await engine.goto(f"{site_server}/experience-lab/select-all.html")
    ctx = make_ctx(engine, profile, JOB, AgentMode.DRY_RUN, FakeGate())
    report = await ctx.form_filler().fill(engine.page)
    checked = await engine.page.evaluate(
        "() => [...document.querySelectorAll('input[name=stack]:checked')].map(e => e.parentElement.innerText.trim())")
    assert checked == ["React", "TypeScript, JavaScript"], (checked, [(i.question, i.status, i.note) for i in report.items])
    assert not report.unresolved


async def test_validation_messages_inside_web_components_are_read(engine, site_server):
    await engine.goto(f"{site_server}/submit-lab/shadow-error.html")
    assert await collect_errors(engine.page) == []
    await engine.page.fill("#nm", "Asha Verma")
    await engine.page.click("button[type=submit]")
    assert await collect_errors(engine.page) == ["Please fill in this field."]
    assert [f.label for f in await scan_fields(engine.page)][:1] == ["Full name *"]


async def test_a_fields_own_label_is_never_replaced_by_its_example_placeholder(engine, site_server):
    """Two forms on one page each had a "Full Name" box; the scanner called them 'Alex Morgan' and 'Dr. Sarah Jenkins'
    (their placeholders), and those were asked as questions."""
    await engine.goto(f"{site_server}/experience-lab/two-forms.html")
    fields = await scan_fields(engine.page)
    assert [f.label for f in fields] == ["Full Name", "Email Address", "Contact Number", "Upload Resume",
                                         "Full Name", "Email Address"]
    assert all(f.own_label for f in fields if f.label != "Contact Number")


async def test_headings_shared_by_unlabelled_fields_still_fall_back_to_placeholders(engine, site_server):
    await engine.goto(f"{site_server}/careers/placeholder/form.html")
    labels = [f.label for f in await scan_fields(engine.page)]
    assert len(labels) == len(set(labels)) and labels, labels  # every field can be told apart


async def test_a_site_that_does_not_load_is_a_failure_not_a_question_for_you(engine, site_server, profile):
    res, ctx, _ = await _apply(engine, profile, f"{site_server}/careers/down/index.html")
    assert res.outcome == ApplyOutcome.FAILED, (res.outcome, res.message)
    assert "did not load" in res.message and "chromewebdata" not in res.message
    assert not res.submit_attempted and ctx.submits == []


# ------------------------------------------------------------------ CAPTCHAs stay yours - and are waited for

async def test_inline_captcha_gate_waits_until_the_widget_is_solved(engine, site_server, profile):
    """The 'solved?' check looked only for challenge pages, so an inline "I'm not a robot" box counted as solved at
    once and the form was sent unsolved (the sites answered "CAPTCHA cannot be empty")."""
    await engine.goto(f"{site_server}/careers/captcha/form.html")
    assert await engine.captcha_widget_present() and not await engine.captcha_widget_solved()
    await engine.page.check("#robot")
    assert await engine.captcha_widget_solved()


async def test_unsolved_inline_captcha_parks_the_job_and_sends_nothing(engine, site_server, profile):
    seen: list[bool] = []

    async def nobody_there(page):  # the dashboard's gate polls the detector; nobody solves anything
        seen.append(await engine.captcha_widget_solved(page))
        return GateResult.TIMEOUT

    gate = FakeGate([nobody_there])
    ctx = make_ctx(engine, profile, JOB, AgentMode.AUTO, gate=gate, auto_wait=30)
    await engine.goto(f"{site_server}/careers/captcha/form.html")
    res = await apply_external(engine, engine.page, ctx)
    assert res.outcome == ApplyOutcome.MANUAL_ACTION_REQUIRED and "CAPTCHA" in res.message, res.message
    assert seen == [False] and ctx.submits == [] and not res.submit_attempted
    assert await engine.page.locator("#robot").is_checked() is False  # never ticked for you


async def test_a_sum_to_type_is_a_captcha_and_is_never_answered(engine, site_server, profile):
    gate = FakeGate([GateResult.TIMEOUT])
    ctx = make_ctx(engine, profile, JOB, AgentMode.AUTO, gate=gate, auto_wait=30)
    await engine.goto(f"{site_server}/careers/captcha/sum.html")
    res = await apply_external(engine, engine.page, ctx)
    assert res.outcome == ApplyOutcome.MANUAL_ACTION_REQUIRED, (res.outcome, res.message)
    assert "CAPTCHA" in res.message and "'Submit' disabled until it is solved" in res.message
    assert gate.calls and gate.calls[0][0] == "captcha"
    assert await engine.page.input_value("#c") == "" and await engine.page.evaluate("() => window.SUBMITS") == 0
    assert await engine.page.input_value("#n") == "Asha Verma"  # the rest of the form is ready for you


async def test_after_you_solve_the_sum_the_application_is_sent(engine, site_server, profile):
    async def user_solves(page):
        await page.fill("#c", "7")
        await page.dispatch_event("#c", "keyup")
        return GateResult.RESOLVED

    ctx = make_ctx(engine, profile, JOB, AgentMode.AUTO, gate=FakeGate([user_solves]), auto_wait=30)
    await engine.goto(f"{site_server}/careers/captcha/sum.html")
    res = await apply_external(engine, engine.page, ctx)
    assert res.outcome == ApplyOutcome.APPLIED, (res.outcome, res.message)
    assert len(ctx.submits) == 1


async def test_a_submit_button_that_stays_disabled_is_said_so(engine, site_server, profile):
    ctx = make_ctx(engine, profile, JOB, AgentMode.AUTO, gate=FakeGate(), auto_wait=0)
    await engine.goto(f"{site_server}/careers/captcha/locked.html")
    res = await apply_external(engine, engine.page, ctx)
    assert res.outcome == ApplyOutcome.MANUAL_ACTION_REQUIRED
    assert "'SUBMIT APPLICATION' button stays disabled" in res.message and "could not find" not in res.message
    assert await engine.page.evaluate("() => window.SUBMITS") == 0


# ------------------------------------------------------------------ "Careers" in the title does not make a form an application

@pytest.mark.parametrize("path,is_application_address", [
    ("/apply", True), ("/careers/apply", True), ("/apply.php", True), ("/careers-form.html", True), ("/job-application/", True),
    ("/careers/apply?role=frontend-engineer", True), ("/ats/unknown/apply.html", True), ("/candidate/new", True),
    ("/careers", False), ("/careers.html", False), ("/career/frontend-developer-profile", False), ("/contact", False),
    ("/", False), ("/appliances", False), ("/happy-clients", False),
])
def test_only_the_address_of_an_application_page_counts(path, is_application_address):
    from app.navigator.agent import _APPLY_URL

    assert bool(_APPLY_URL.search(path)) is is_application_address


def test_a_contact_forms_acknowledgement_never_confirms_without_a_resume():
    before = PageState(url="https://co.test/careers", fields=["name", "email", "message"], submit_visible=True, submit_enabled=True)
    inquiry = dict(text="Thank you for your inquiry! We will get back to you within 48 hours.", kinds=["thanks"], region="page")
    after = PageState(url=before.url, fields=[], messages=[inquiry])
    bare = decide(before, after, final=True, resume_attached=False)
    assert bare.state == SubmissionState.UNCERTAIN and "acknowledged a message, not an application" in bare.message
    assert decide(before, after, final=True, resume_attached=True).state == SubmissionState.CONFIRMED  # sent with a résumé
    applied = PageState(url=before.url, fields=[], messages=[
        dict(text="Your application has been received.", kinds=["success"], region="alert")])
    assert decide(before, applied, final=True, resume_attached=False).state == SubmissionState.CONFIRMED  # the site says so
    thanks = PageState(url="https://co.test/thank-you", fields=[], messages=[dict(text="Thank You!", kinds=["thanks"], region="heading")])
    assert decide(before, thanks, final=True, resume_attached=False).state == SubmissionState.CONFIRMED  # (an application form)


# ------------------------------------------------------------------ typeahead whose suggestions arrive late

async def test_city_typeahead_waits_for_its_suggestions_and_picks_the_profile_city(engine, site_server, profile):
    await engine.goto(f"{site_server}/experience-lab/city-typeahead.html")
    ctx = make_ctx(engine, profile, JOB, AgentMode.DRY_RUN, FakeGate())
    report = await ctx.form_filler().fill(engine.page)
    chosen = await engine.page.evaluate("() => window.CITY")
    assert chosen.startswith("Bengaluru"), (chosen, [(i.question, i.status, i.note) for i in report.items])
    assert not report.unresolved and await engine.page.evaluate("() => window.LOOKUPS") >= 1


async def test_the_whole_application_goes_through_with_the_slow_city_box(engine, site_server, profile):
    res, ctx, submits = await _apply(engine, profile, f"{site_server}/experience-lab/city-typeahead.html?delay=2500")
    assert res.outcome == ApplyOutcome.APPLIED, (res.outcome, res.message)
    assert submits == 1 and (await engine.page.evaluate("() => window.CITY")).startswith("Bengaluru")
    assert not ctx.gate.calls  # nobody was asked for a city the profile has


def test_placeholders_in_a_suggestion_list_are_not_suggestions():
    from app.browser.forms import _NO_SUGGESTION

    for text in ("No results found", "Loading...", "Searching", "Type to search", "No options", "no matches"):
        assert _NO_SUGGESTION.match(text), text
    for text in ("Noida, Uttar Pradesh, India", "Pune, Maharashtra, India", "Norway"):
        assert not _NO_SUGGESTION.match(text), text


async def test_suggestions_drawn_without_list_markup_are_found_by_what_they_say(engine, site_server, profile):
    """SmartRecruiters "City" (run #27): the suggestions are plain elements inside a web component - no role, no <li>.
    The typed text was thrown away on blur and the site answered "Please provide your place of residence"."""
    res, ctx, submits = await _apply(engine, profile, f"{site_server}/experience-lab/city-typeahead.html?markup=plain")
    assert res.outcome == ApplyOutcome.APPLIED, (res.outcome, res.message)
    assert (await engine.page.evaluate("() => window.CITY")).startswith("Bengaluru") and submits == 1
    assert not ctx.gate.calls


# ------------------------------------------------------------------ the résumé goes to the résumé upload

@pytest.mark.parametrize("avatar", ["accept", "bare"])
async def test_resume_is_never_put_into_the_profile_picture_upload(engine, site_server, profile, avatar):
    res, ctx, submits = await _apply(engine, profile, f"{site_server}/experience-lab/two-uploads.html?avatar={avatar}")
    assert res.outcome == ApplyOutcome.APPLIED, (res.outcome, res.message)
    sent = await engine.page.evaluate("() => window.SENT")
    assert sent == {"avatar": 0, "resume": "resume-frontend.pdf"} and submits == 1
    assert not ctx.gate.calls


@pytest.mark.parametrize("accept,pictures", [
    ("image/*", True), ("image/png, image/jpeg", True), (".png,.jpg,.JPEG", True), (".bmp, .gif, image/webp", True),
    ("", False), (".pdf,.doc,.docx", False), ("application/pdf", False), ("image/*,.pdf", False), (".pdf, .png", False),
])
def test_images_only(accept, pictures):
    from app.browser.forms import images_only

    assert images_only(accept) is pictures


# ------------------------------------------------------------------ the whole real SmartRecruiters flow, replayed

async def test_smartrecruiters_two_step_application_as_it_really_is(engine, site_server, profile):
    """Nagarro via SmartRecruiters (runs #26-#29): picture upload, late typeahead, a second step drawn after its
    Submit button. Every one of them stopped the real application; together they must go through - once."""
    res, ctx, submits = await _apply(engine, profile, f"{site_server}/ats/smartrecruiters/apply-slow.html")
    assert res.outcome == ApplyOutcome.APPLIED and res.ats == "smartrecruiters", (res.outcome, res.message, ctx.events)
    sent = await engine.page.evaluate("() => window.SENT")
    assert sent["avatar"] == 0 and sent["resume"] == "resume-frontend.pdf"  # the résumé is in the résumé upload
    assert sent["comp"] == "2800000" and sent["expect"] and sent["lead"] == "30"  # step 2 was seen and answered
    # (the compensation box is a number box next to "INR": the annual amount in rupees, not "28 LPA")
    assert sent["city"].startswith("Bengaluru"), sent  # the second typeahead's own list was used, not the first one's
    assert submits == 1 and len(ctx.submits) == 1 and not ctx.gate.calls  # one submit, nobody asked


# ------------------------------------------------------------------ a refused submit sent nothing

async def test_a_submit_the_form_itself_refused_is_not_recorded_as_sent(engine, site_server, profile):
    """SmartRecruiters (run #29): "Value is required" - the form refused the click, nothing left the browser, yet the
    record kept "a submit was attempted": Retry was blocked and the next attempt was closed as already applied."""
    ctx = make_ctx(engine, profile, JOB, AgentMode.AUTO, gate=FakeGate([]), auto_wait=0)
    await engine.goto(f"{site_server}/careers/captcha/form.html")  # its form refuses until the box is ticked
    await engine.page.evaluate("() => document.querySelector('.g-recaptcha').className = 'robot-box'")  # (no gate: a plain refusal)
    res = await apply_external(engine, engine.page, ctx)
    assert res.outcome == ApplyOutcome.MANUAL_ACTION_REQUIRED, (res.outcome, res.message)
    assert "Please complete the CAPTCHA" in res.message  # the site's own words
    assert res.nothing_sent and not res.submit_attempted


# ------------------------------------------------------------------ another service's sign-in page is never filled

async def test_nothing_is_typed_into_an_identity_providers_sign_in_page(engine, site_server, profile):
    gate = FakeGate([GateResult.TIMEOUT])
    ctx = make_ctx(engine, profile, JOB, AgentMode.AUTO, gate=gate, auto_wait=30)
    await engine.goto(f"{site_server}/careers/sso/job.html")
    res = await apply_external(engine, engine.page, ctx)
    assert res.outcome == ApplyOutcome.MANUAL_ACTION_REQUIRED and "sign in with Google" in res.message, res.message
    assert await engine.page.input_value("#identifierId") == ""  # your e-mail address was not entered
    assert await engine.page.evaluate("() => window.NEXT") == 0 and ctx.submits == []
    assert gate.calls and gate.calls[0][0] == "account"
    assert not any(t == "question" for t, _ in ctx.events)  # it was never treated as a question to answer


@pytest.mark.parametrize("url,provider", [
    ("https://accounts.google.com/v3/signin/identifier?continue=https%3A%2F%2Fdocs.google.com%2Fforms", "Google"),
    ("https://accounts.google.co.in/signin", "Google"), ("https://login.microsoftonline.com/common/oauth2/authorize", "Microsoft"),
    ("https://login.live.com/login.srf", "Microsoft"), ("https://appleid.apple.com/auth/authorize", "Apple"),
    ("https://acme.okta.com/login/login.htm", "Okta"), ("https://www.linkedin.com/uas/login?session_redirect=x", "Linkedin"),
    ("https://github.com/login", "Github"),
    # not sign-in pages
    ("https://docs.google.com/forms/d/e/abc/viewform", ""), ("https://careers.google.com/jobs/results/1", ""),
    ("https://www.linkedin.com/jobs/view/123", ""), ("https://github.com/acme/careers", ""),
    ("https://acme.example/careers/login-help", ""), ("https://jobs.smartrecruiters.com/oneclick-ui/company/x", ""),
])
def test_identity_provider_pages(url, provider):
    from app.navigator.agent import identity_provider

    assert identity_provider(url) == provider


# ------------------------------------------------------------------ dropdown widgets without ARIA (Semantic UI)

async def test_search_dropdowns_are_chosen_from_their_list_not_typed_into(engine, site_server, profile):
    """app.intellohire.com (run #35): the city and the preferred locations were typed into the dropdowns' search
    boxes; nothing was chosen and the form refused ("Select candidate current city")."""
    await engine.goto(f"{site_server}/experience-lab/semantic-dropdown.html")
    fields = {f.label: f for f in await scan_fields(engine.page)}
    assert fields["Current city"].type == "combobox" and fields["Looking for jobs in"].multiple
    assert all(fields[name].required for name in ("Name", "Email", "Current city", "Looking for jobs in"))  # wrapper class
    years = fields["Total experience (in yrs)"]
    assert years.widget and "7 Years (7.0)" in years.option_labels and "Fresher" in years.option_labels  # the wrapped <select>
    assert fields["Looking for jobs in"].option_labels[:2] == ["Bengaluru", "Pune"] and not fields["Current city"].options
    res, ctx, submits = await _apply(engine, profile, f"{site_server}/experience-lab/semantic-dropdown.html")
    assert res.outcome == ApplyOutcome.APPLIED, (res.outcome, res.message)
    sent = await engine.page.evaluate("() => window.SENT")
    assert sent == {"city": "Bengaluru", "pref": "Bengaluru,Remote", "exp": "7 Years (7.0)"}, sent  # never "Fresher"
    assert submits == 1 and not ctx.gate.calls
    # "Key Skills" offered nothing to choose: its box was cleared and left - never Escape, which closes the dialog
    # and with it the whole form (run #37 reopened and refilled the form for 15 minutes)
    assert await engine.page.evaluate("() => window.ESCAPED") == 0


async def test_resume_is_attached_again_when_the_form_is_redrawn_empty(engine, site_server, profile):
    res, ctx, submits = await _apply(engine, profile, f"{site_server}/experience-lab/resume-reset.html")
    assert res.outcome == ApplyOutcome.APPLIED, (res.outcome, res.message)
    assert submits == 1 and await engine.page.evaluate("() => window.REFUSALS") == 1  # the phone format once - not the résumé


async def test_resume_is_chosen_again_when_the_site_says_it_is_missing(engine, site_server, profile):
    """app.intellohire.com (run #39): the box still showed the file, the site had dropped its own copy and answered
    "Please attach you resume" - twice, and the application was left to the user."""
    res, ctx, submits = await _apply(engine, profile, f"{site_server}/experience-lab/resume-reset.html?mode=token")
    assert res.outcome == ApplyOutcome.APPLIED, (res.outcome, res.message)
    assert submits == 1 and await engine.page.evaluate("() => window.UPLOADS") == 2  # chosen again once - never in a loop
    assert await engine.page.evaluate("() => window.REFUSALS") == 2  # the phone format, then the missing résumé


async def test_resume_goes_through_the_sites_upload_button(engine, site_server, profile):
    """A hidden file input behind an "Upload Resume" button whose click wires up the upload (intellohire)."""
    res, ctx, submits = await _apply(engine, profile, f"{site_server}/experience-lab/upload-button.html")
    assert res.outcome == ApplyOutcome.APPLIED, (res.outcome, res.message)
    assert await engine.page.evaluate("() => window.UPLOADS") == 1 and submits == 1
    assert await engine.page.evaluate("() => document.getElementById('resume_hash').value") == "hash-1"
    assert not ctx.gate.calls


async def test_unbound_upload_button_is_used_once_the_site_says_the_resume_is_missing(engine, site_server, profile):
    """The button does not name its input: the file goes into the input first (the usual way), the site answers
    "Please attach you resume", and only then its own Upload button is used - never the Dropbox one beside it."""
    res, ctx, submits = await _apply(engine, profile, f"{site_server}/experience-lab/upload-button.html?near=1")
    assert res.outcome == ApplyOutcome.APPLIED, (res.outcome, res.message)
    assert await engine.page.evaluate("() => [window.UPLOADS, window.DROPBOX || 0]") == [1, 0] and submits == 1
    assert not ctx.gate.calls


async def test_landing_on_the_sites_login_page_after_submit_is_not_a_confirmation(engine, site_server, profile):
    """The form is sent, then the site shows its login page (intellohire): not APPLIED, sent once, and nothing is
    typed into the login form - no OTP is requested in your name."""
    res, ctx, _ = await _apply(engine, profile, f"{site_server}/submit-lab/login-after.html")
    assert res.outcome == ApplyOutcome.MANUAL_ACTION_REQUIRED, (res.outcome, res.message)
    assert "sign-in page" in res.message and res.submit_attempted and not res.nothing_sent
    assert "sign_in_page" in " ".join(res.evidence) and "submit_request_accepted" not in " ".join(res.evidence)
    assert len(ctx.submits) == 1
    assert engine.page.url.endswith("/submit-lab/login.html")
    assert await engine.page.evaluate("() => [document.querySelector('[name=phone]').value, window.OTP_REQUESTS]") == ["", 0]
