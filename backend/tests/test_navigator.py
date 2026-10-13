"""Generic navigator on company career sites it has no adapter for (tests/fixtures/sites/careers/*).

Each mock site reproduces a real-world situation: cookie banners, header search / footer newsletter
noise, multi-step SPA forms, guest-vs-sign-in walls, login-only walls, account creation with email
verification, promotional popups, iframe-embedded forms, inline CAPTCHA, closed jobs, already-applied
pages, unconventional buttons (local-AI fallback), post-submit questionnaires and new tabs.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.ai.provider import OllamaProvider
from app.ats.external import apply_external
from app.browser.engine import BrowserEngine
from app.engine.context import GateResult
from app.models import AgentMode
from app.navigator.agent import ApplicationNavigator, application_listed
from app.navigator.planner import PageReasoner
from app.portals.types import ApplyOutcome, JobPosting
from tests.helpers import FakeGate, answers_by_question, make_ctx

pytestmark = pytest.mark.browser

JOB = JobPosting(portal="naukri", external_id="nav-1", url="https://example.test/job", title="Senior Frontend Engineer",
                 company="Acme", location="Bengaluru",
                 description="Build React and TypeScript web apps. 6+ years of experience.")


@pytest.fixture
async def engine(site_server, tmp_path):
    e = BrowserEngine(headless=True, user_data_dir=tmp_path / "p", screenshot_dir=tmp_path / "s", human_delays=False,
                      persistent=False)
    await e.start()
    yield e
    await e.stop()


async def _open(engine, url):
    await engine.goto(url)
    return engine.page


async def _apply(engine, url, ctx):
    page = await _open(engine, url)
    return await apply_external(engine, page, ctx)


# ------------------------------------------------------------------ multi-step SPA with noise

async def test_multistep_company_site_auto_apply_with_question_gate(engine, site_server, profile):
    gate = FakeGate([answers_by_question({"passport": "Yes"})])
    ctx = make_ctx(engine, profile, JOB, AgentMode.AUTO, gate=gate, auto_wait=30)
    res = await _apply(engine, f"{site_server}/careers/acme/job.html", ctx)
    assert res.outcome == ApplyOutcome.APPLIED, res.message
    assert "Thank you for applying" in res.confirmation_text or "submitted" in res.confirmation_text.lower()
    data = await engine.page.evaluate("() => window.__submitted")
    assert data["first_name"] == "Asha" and data["email"] == profile.personal.email
    assert data["resume"].endswith(".pdf") and data["years"] == "7" and data["notice"] == "30 days"
    assert data["auth"] == "yes" and data["passport"] == "yes" and data["confirm"] is True
    asked = [q.question.lower() for q, _a in ctx.answers_log]
    assert not any("job alerts" in q or "search" in q for q in asked)  # footer newsletter / header search untouched
    # the written question was answered from the profile + job description, filled and verified - not asked
    assert "React" in data["why"] and "Acme" in data["why"]
    why = next(a for q, a in ctx.answers_log if q.question.startswith("Why are you interested"))
    assert why.mode == "composed" and why.verified and "job.description" in why.sources
    log = next(m for t, m in ctx.events if t == "question" and "Why are you interested" in m)
    assert "Answer source: profile + job description" in log and "Verified" in log
    # the unknown question was asked once, with its options, then remembered for future forms
    kinds = [k for k, _ in gate.calls]
    assert kinds == ["questions"]
    q = gate.payloads[0]["questions"]
    assert len(q) == 1 and "passport" in q[0]["question"].lower() and q[0]["options"] == ["Yes", "No"]
    assert ("Do you hold a valid passport?", "Yes") in ctx.remembered
    assert len(ctx.submits) == 1


async def test_multistep_dry_run_never_submits(engine, site_server, profile):
    ctx = make_ctx(engine, profile, JOB, AgentMode.DRY_RUN)
    res = await _apply(engine, f"{site_server}/careers/acme/job.html", ctx)
    assert res.outcome == ApplyOutcome.DRY_RUN, res.message
    assert ctx.submits == []
    assert await engine.page.evaluate("() => window.__submitted") is None
    assert any("passport" in u.lower() for u in res.unresolved)


async def test_multistep_auto_without_answer_parks_job(engine, site_server, profile):
    ctx = make_ctx(engine, profile, JOB, AgentMode.AUTO, gate=FakeGate([]))  # nobody answers
    res = await _apply(engine, f"{site_server}/careers/acme/job.html", ctx)
    assert res.outcome == ApplyOutcome.MANUAL_ACTION_REQUIRED
    assert "passport" in res.message.lower() and ctx.submits == []
    assert res.resume_url and "apply.html" in res.resume_url  # a retry continues on the company site


async def test_review_mode_waits_for_approval(engine, site_server, profile):
    gate = FakeGate([answers_by_question({"passport": "No"}), GateResult.APPROVE])
    ctx = make_ctx(engine, profile, JOB, AgentMode.REVIEW, gate=gate)
    res = await _apply(engine, f"{site_server}/careers/acme/job.html", ctx)
    assert res.outcome == ApplyOutcome.APPLIED, res.message
    assert [k for k, _ in gate.calls] == ["questions", "review"]
    assert (await engine.page.evaluate("() => window.__submitted"))["passport"] == "no"


async def test_review_mode_reject(engine, site_server, profile):
    gate = FakeGate([answers_by_question({"passport": "No"}), GateResult.REJECT])
    ctx = make_ctx(engine, profile, JOB, AgentMode.REVIEW, gate=gate)
    res = await _apply(engine, f"{site_server}/careers/acme/job.html", ctx)
    assert res.outcome == ApplyOutcome.REJECTED and ctx.submits == []


# ------------------------------------------------------------------ accounts

async def test_guest_option_preferred_over_sign_in(engine, site_server, profile):
    ctx = make_ctx(engine, profile, JOB, AgentMode.AUTO)
    res = await _apply(engine, f"{site_server}/careers/guest/job.html", ctx)
    assert res.outcome == ApplyOutcome.APPLIED, res.message
    assert "thank you for your application" in res.confirmation_text.lower()


async def test_login_wall_pauses_for_you_and_continues(engine, site_server, profile):
    async def user_signs_in(page):
        await page.evaluate("() => localStorage.setItem('gamma_account', 'asha.verma.dev@gmail.com')")
        await page.goto(page.url.replace("signin.html", "form.html"))
        return GateResult.RESOLVED

    gate = FakeGate([user_signs_in])
    ctx = make_ctx(engine, profile, JOB, AgentMode.AUTO, gate=gate, auto_wait=30)
    res = await _apply(engine, f"{site_server}/careers/login/job.html", ctx)
    assert res.outcome == ApplyOutcome.APPLIED, res.message
    assert gate.calls[0][0] == "account" and "sign in" in gate.calls[0][1].lower()


async def test_login_wall_in_auto_mode_without_you_is_parked(engine, site_server, profile):
    ctx = make_ctx(engine, profile, JOB, AgentMode.AUTO, gate=FakeGate([]))
    res = await _apply(engine, f"{site_server}/careers/login/job.html", ctx)
    assert res.outcome == ApplyOutcome.MANUAL_ACTION_REQUIRED and "sign in" in res.message.lower()
    assert not res.submit_attempted


async def test_creates_account_then_waits_for_email_verification(engine, site_server, profile, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "ats_account_password", "Correct-Horse-1")

    async def user_verifies(page):
        await page.goto(page.url.rsplit("/", 1)[0] + "/form.html")
        return GateResult.RESOLVED

    gate = FakeGate([user_verifies])
    ctx = make_ctx(engine, profile, JOB, AgentMode.AUTO, gate=gate, auto_wait=30)
    res = await _apply(engine, f"{site_server}/careers/login/job.html", ctx)
    assert res.outcome == ApplyOutcome.APPLIED, res.message
    assert [k for k, _ in gate.calls] == ["verify"]
    types = [t for t, _ in ctx.events]
    assert "account" in types


async def test_existing_account_signs_in_with_configured_password(engine, site_server, profile, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "ats_account_password", "Correct-Horse-1")
    page = await _open(engine, f"{site_server}/careers/login/job.html")
    await page.evaluate("() => localStorage.setItem('gamma_account', 'asha.verma.dev@gmail.com')")
    ctx = make_ctx(engine, profile, JOB, AgentMode.AUTO, gate=FakeGate([]))
    res = await apply_external(engine, page, ctx)
    assert res.outcome == ApplyOutcome.APPLIED, res.message


async def test_dry_run_never_creates_accounts(engine, site_server, profile, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "ats_account_password", "Correct-Horse-1")
    page = await _open(engine, f"{site_server}/careers/login/register.html")
    ctx = make_ctx(engine, profile, JOB, AgentMode.DRY_RUN)
    res = await apply_external(engine, page, ctx)
    assert res.outcome == ApplyOutcome.DRY_RUN and "account" in res.message.lower()
    assert await page.evaluate("() => localStorage.getItem('gamma_account')") is None


# ------------------------------------------------------------------ page situations

async def test_promotional_popup_is_closed(engine, site_server, profile):
    ctx = make_ctx(engine, profile, JOB, AgentMode.AUTO)
    res = await _apply(engine, f"{site_server}/careers/popup/job.html", ctx)
    assert res.outcome == ApplyOutcome.APPLIED, res.message


async def test_iframe_embedded_form(engine, site_server, profile):
    ctx = make_ctx(engine, profile, JOB, AgentMode.AUTO)
    page = await _open(engine, f"{site_server}/careers/frame/job.html")
    res = await apply_external(engine, page, ctx)
    assert res.outcome == ApplyOutcome.APPLIED, res.message
    data = await page.evaluate("() => window.__frameSubmitted")
    assert data["name"] == "Asha Verma" and data["np"] == "Up to 30 days" and data["gender"] == "f"
    assert data["cv"].endswith(".pdf")


async def test_inline_captcha_is_left_to_you_before_submit(engine, site_server, profile):
    async def user_solves(page):
        await page.check("#robot")
        return GateResult.RESOLVED

    gate = FakeGate([user_solves])
    ctx = make_ctx(engine, profile, JOB, AgentMode.AUTO, gate=gate, auto_wait=30)
    res = await _apply(engine, f"{site_server}/careers/captcha/form.html", ctx)
    assert res.outcome == ApplyOutcome.APPLIED, res.message
    assert gate.calls[0][0] == "captcha"
    assert await engine.page.locator("#robot").count() == 0  # confirmation replaced the form


async def test_closed_job_is_expired(engine, site_server, profile):
    ctx = make_ctx(engine, profile, JOB, AgentMode.AUTO)
    res = await _apply(engine, f"{site_server}/careers/closed/job.html", ctx)
    assert res.outcome == ApplyOutcome.EXPIRED


async def test_already_applied_page(engine, site_server, profile):
    ctx = make_ctx(engine, profile, JOB, AgentMode.AUTO)
    res = await _apply(engine, f"{site_server}/careers/applied/job.html", ctx)
    assert res.outcome == ApplyOutcome.ALREADY_APPLIED


async def test_post_submit_questionnaire_then_confirmation(engine, site_server, profile):
    gate = FakeGate([GateResult.APPROVE])
    ctx = make_ctx(engine, profile, JOB, AgentMode.REVIEW, gate=gate)
    res = await _apply(engine, f"{site_server}/careers/multi/form.html", ctx)
    assert res.outcome == ApplyOutcome.APPLIED, res.message
    assert [k for k, _ in gate.calls] == ["review"]  # approved once for the whole application
    assert len(ctx.submits) == 2


async def test_apply_link_opening_new_tab(engine, site_server, profile):
    ctx = make_ctx(engine, profile, JOB, AgentMode.AUTO)
    page = await _open(engine, f"{site_server}/careers/newtab/job.html")
    res = await apply_external(engine, page, ctx)
    assert res.outcome == ApplyOutcome.APPLIED, res.message
    assert len(engine.context.pages) == 1  # the extra tab was closed afterwards


# ------------------------------------------------------------------ local AI fallback

def _planner_provider(pick_text: str):
    """Fake Ollama that clicks the element whose label contains ``pick_text`` (read from the prompt)."""
    import re

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "test-model"}]})
        body = json.loads(request.content)
        prompt = body["messages"][-1]["content"]
        calls.append(prompt)
        m = re.search(r"\[(a\d+)\][^\n]*" + re.escape(pick_text), prompt)
        out = {"page_type": "job_description", "action": "click", "element_id": m.group(1) if m else "a999",
               "reason": "starts the application", "confidence": 0.8}
        return httpx.Response(200, json={"message": {"role": "assistant", "content": json.dumps(out)}})

    provider = OllamaProvider(base_url="http://ollama.test", model="test-model", enabled=True,
                              transport=httpx.MockTransport(handler))
    return provider, calls


async def test_unconventional_button_uses_local_ai(engine, site_server, profile):
    provider, calls = _planner_provider("Let's go")
    ctx = make_ctx(engine, profile, JOB, AgentMode.AUTO)
    page = await _open(engine, f"{site_server}/careers/odd/job.html")
    nav = ApplicationNavigator(engine, ctx, reasoner=PageReasoner(provider))
    res = await nav.run(page)
    assert res.outcome == ApplyOutcome.APPLIED, res.message
    assert calls and "Let's go" in calls[0]
    assert "About" not in calls[0].split("CLICKABLE ELEMENTS:")[1].split("RECENT ACTIONS")[0]  # nav noise hidden


async def test_ai_inventing_an_element_is_ignored(engine, site_server, profile):
    provider, _calls = _planner_provider("no such button")
    ctx = make_ctx(engine, profile, JOB, AgentMode.AUTO, gate=FakeGate([]))
    page = await _open(engine, f"{site_server}/careers/odd/job.html")
    nav = ApplicationNavigator(engine, ctx, reasoner=PageReasoner(provider))
    res = await nav.run(page)
    assert res.outcome == ApplyOutcome.MANUAL_ACTION_REQUIRED and ctx.submits == []


def test_application_list_evidence():
    body = "My applications\nSenior Frontend Engineer  Bengaluru  Submitted 2 minutes ago\nUI Engineer Draft"
    assert "Submitted" in application_listed(body, "Senior Frontend Engineer") or \
        "submitted" in application_listed(body, "Senior Frontend Engineer")
    assert application_listed("Senior Frontend Engineer - Apply now", "Senior Frontend Engineer") == ""


# ------------------------------------------------------------------ patterns seen on real career sites

REACT_JOB = JobPosting(portal="naukri", external_id="nav-2", url="https://example.test/j2", title="Senior React Developer",
                       company="Nexus", location="Bengaluru", description="React, Redux, TypeScript")


async def test_placeholder_labelled_form_and_honeypot(engine, site_server, profile):
    ctx = make_ctx(engine, profile, REACT_JOB, AgentMode.AUTO)
    res = await _apply(engine, f"{site_server}/careers/placeholder/form.html", ctx)
    assert res.outcome == ApplyOutcome.APPLIED, res.message
    answers = {q.question: a.value for q, a in ctx.answers_log}
    assert answers["First Name"] == "Asha" and answers["Phone Number"].endswith("9812345678")
    assert answers["Total Experience"] == "6-10 Years" and answers["Notice Period"] == "30 Days"
    assert not any("honeypot" in q.lower() for q in answers)  # the anti-bot trap is never touched
    assert await engine.page.evaluate("() => sessionStorage.getItem('hp')") == ""


async def test_job_listing_picks_this_jobs_apply_button(engine, site_server, profile):
    ctx = make_ctx(engine, profile, REACT_JOB, AgentMode.AUTO)
    res = await _apply(engine, f"{site_server}/careers/listing/jobs.html", ctx)
    assert res.outcome == ApplyOutcome.APPLIED, res.message  # went to the React job's form, not the designer's


async def test_dead_apply_buttons_are_not_clicked_in_a_loop(engine, site_server, profile):
    ctx = make_ctx(engine, profile, REACT_JOB, AgentMode.AUTO, gate=FakeGate([]))
    res = await _apply(engine, f"{site_server}/careers/listing/dead.html", ctx)
    assert res.outcome == ApplyOutcome.MANUAL_ACTION_REQUIRED
    assert "did not open an application form" in res.message
    clicks = [m for t, m in ctx.events if t == "step" and m.startswith("Clicked")]
    assert len(clicks) == 1


async def test_email_only_application_is_explained(engine, site_server, profile):
    ctx = make_ctx(engine, profile, REACT_JOB, AgentMode.AUTO)
    res = await _apply(engine, f"{site_server}/careers/email/job.html", ctx)
    assert res.outcome == ApplyOutcome.MANUAL_ACTION_REQUIRED and "hr@softsell.test" in res.message


async def test_js_only_apply_button_and_save_in_dialog(engine, site_server, profile):
    ctx = make_ctx(engine, profile, REACT_JOB, AgentMode.AUTO)
    res = await _apply(engine, f"{site_server}/careers/spa/job.html", ctx)
    assert res.outcome == ApplyOutcome.APPLIED, res.message
    data = await engine.page.evaluate("() => window.__spa")
    assert data["name"] == "Asha Verma" and data["exp"] == "7" and data["np"] == "30" and data["cv"] == 1
    assert len(ctx.submits) == 1  # "Save" in the application dialog went through the submit gate


async def test_js_only_save_is_never_clicked_in_dry_run(engine, site_server, profile):
    ctx = make_ctx(engine, profile, REACT_JOB, AgentMode.DRY_RUN)
    res = await _apply(engine, f"{site_server}/careers/spa/job.html", ctx)
    assert res.outcome == ApplyOutcome.DRY_RUN and ctx.submits == []
    assert await engine.page.evaluate("() => window.__spa") is None


async def test_unavailable_posting(engine, site_server, profile):
    ctx = make_ctx(engine, profile, REACT_JOB, AgentMode.AUTO)
    res = await _apply(engine, f"{site_server}/careers/gone/job.html", ctx)
    assert res.outcome == ApplyOutcome.EXPIRED and "Access Denied" in res.message


async def test_custom_dropdown_and_typeahead_widgets(engine, site_server, profile):
    """A button-style dropdown (aria-haspopup=listbox) and a city typeahead: found, filled, verified, submitted."""
    ctx = make_ctx(engine, profile, REACT_JOB, AgentMode.AUTO)
    res = await _apply(engine, f"{site_server}/careers/widget/form.html", ctx)
    assert res.outcome == ApplyOutcome.APPLIED, res.message
    data = await engine.page.evaluate("() => window.__widget")
    assert data["np"] == "Up to 30 days" and data["city"].startswith("Bengaluru") and "React" in data["why"]


async def test_the_same_ai_advice_is_not_followed_a_third_time(engine, site_server, profile):
    """limitlessmobil.com (run #26): the local AI chose 'Career' eight times in a row on pages that lead nowhere."""
    provider, calls = _planner_provider("Career")
    ctx = make_ctx(engine, profile, JOB, AgentMode.AUTO, gate=FakeGate([]))
    page = await _open(engine, f"{site_server}/careers/loop/a.html")
    nav = ApplicationNavigator(engine, ctx, reasoner=PageReasoner(provider))
    res = await nav.run(page)
    assert res.outcome == ApplyOutcome.MANUAL_ACTION_REQUIRED and ctx.submits == []
    assert len(calls) == 3, (len(calls), res.message, nav.history, [e for e in ctx.events][-6:])  # twice, then refused
    assert any(t == "ai_loop" and "'Career'" in m for t, m in ctx.events)
