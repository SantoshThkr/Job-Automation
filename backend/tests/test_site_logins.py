"""Signing in on company career sites: with Google (always by you), with a sign-in stored for the site, and what
never happens - a password typed into another service's page, a secret in a log, an account created with the
sign-in of an existing one.

All of it runs against local replica pages. No real account, password or Keychain is involved: the Keychain tool is
replaced by a fake, and every credential below is a made-up test value assembled at run time.
"""

from __future__ import annotations

import logging
import subprocess

import pytest

from app import site_logins as credentials
from app.ats.external import apply_external
from app.browser.engine import BrowserEngine
from app.site_logins import SiteCredential, lookup, normalize_site, same_site, site_keys
from app.engine.context import GateResult
from app.models import AgentMode
from app.navigator import agent as navigator
from app.navigator.agent import identity_provider
from app.portals.types import ApplyOutcome, JobPosting
from app.security import RedactingFilter, register_secret, scrub_html, scrub_secrets
from tests.helpers import FakeGate, make_ctx

JOB = JobPosting(portal="naukri", external_id="login-1", url="https://example.test/job", title="UI Engineer",
                 company="Delta Systems", location="Bengaluru")
USER = "candidate@delta.test"
SECRET = "-".join(["Tr0ub4dor", "delta", "site", "7731"])  # a made-up value, never a real password
OTHER = "-".join(["general", "jobsite", "pw", "5512"])


# ------------------------------------------------------------------ where a sign-in may be used

@pytest.mark.parametrize("address,keys", [
    ("https://apply.careers.acme.com/jobs/1", ["apply.careers.acme.com", "careers.acme.com", "acme.com"]),
    ("www.acme.com", ["acme.com"]),
    ("https://careers.acme.co.in/x", ["careers.acme.co.in", "acme.co.in"]),  # never the public suffix "co.in"
    ("https://acme.wd5.myworkdayjobs.com/en-US/careers", ["acme.wd5.myworkdayjobs.com", "wd5.myworkdayjobs.com", "myworkdayjobs.com"]),
    ("http://127.0.0.1:8123/login", ["127.0.0.1"]), ("localhost", []), ("javascript:alert(1)", []), ("", []),
])
def test_site_keys(address, keys):
    assert site_keys(address) == keys


def test_same_site_and_normalising():
    assert normalize_site("HTTPS://WWW.Careers.Acme.com:443/a?b") == "careers.acme.com"
    assert same_site("https://login.acme.com/x", "https://careers.acme.com/y")
    assert not same_site("https://accounts.google.com/x", "https://careers.acme.com/y")
    assert not same_site("https://evil-acme.com", "https://acme.com")


class _FakeKeychain:
    """Stands in for /usr/bin/security: items by service name, every call recorded."""

    def __init__(self, items: dict[str, tuple[str, str]]):
        self.items = {credentials.SERVICE_PREFIX + k: v for k, v in items.items()}
        self.calls: list[list[str]] = []

    def __call__(self, args, timeout=15.0):
        self.calls.append(list(args))
        if args[0] == "dump-keychain":
            out = "".join(f'keychain: "login"\n    "acct"<blob>="{u}"\n    "svce"<blob>="{s}"\n' for s, (u, _) in self.items.items())
            return subprocess.CompletedProcess(args, 0, out + 'keychain: "login"\n    "svce"<blob>="someone-elses-item"\n', "")
        service = args[args.index("-s") + 1]
        if service not in self.items:
            return subprocess.CompletedProcess(args, 44, "", "The specified item could not be found in the keychain.")
        user, password = self.items[service]
        if args[0] == "delete-generic-password":
            del self.items[service]
            return subprocess.CompletedProcess(args, 0, "", "")
        if "-w" in args:
            return subprocess.CompletedProcess(args, 0, password + "\n", "")
        return subprocess.CompletedProcess(args, 0, f'    "acct"<blob>="{user}"\n    "svce"<blob>="{service}"\n', "")


@pytest.fixture
def keychain(monkeypatch):
    fake = _FakeKeychain({"careers.acme.com": (USER, SECRET)})
    monkeypatch.setattr(credentials, "keychain_available", lambda: True)
    monkeypatch.setattr(credentials, "_run", fake)
    monkeypatch.setattr(credentials.sys, "platform", "darwin")
    monkeypatch.setattr(credentials.shutil, "which", lambda _: "/usr/bin/security")
    return fake


def test_a_stored_sign_in_is_used_on_its_site_and_nowhere_else(keychain, monkeypatch):
    from app.config import get_settings

    cred = lookup("https://apply.careers.acme.com/job/7", default_username="me@profile.test")
    assert cred == SiteCredential("careers.acme.com", USER, SECRET, "keychain") and cred.site_specific
    assert lookup("https://acme.com/", default_username="me@profile.test") is None  # a parent domain is another site
    assert lookup("https://careers.other.com/", default_username="me@profile.test") is None
    assert lookup("not a site", default_username="x") is None
    # the general job-site password (with your profile e-mail) is only what is left when nothing is stored
    monkeypatch.setattr(get_settings(), "ats_account_password", OTHER)
    assert lookup("https://careers.other.com/", default_username="me@profile.test") == SiteCredential(
        "*", "me@profile.test", OTHER, "env")
    assert lookup("https://careers.acme.com/", default_username="me@profile.test").password == SECRET  # stored wins


def test_a_credential_never_shows_its_password(keychain, caplog):
    cred = lookup("https://careers.acme.com/", default_username="x")
    assert SECRET not in repr(cred) and SECRET not in str(cred) and USER in repr(cred)
    # from the moment it is read it is removed from log lines, recorded events and saved pages
    assert scrub_secrets(f"typed {SECRET} into the box") == "typed [redacted] into the box"
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "login with %s failed", (SECRET,), None)
    RedactingFilter([]).filter(record)
    assert SECRET not in record.getMessage() and "[redacted]" in record.getMessage()
    assert not any("-w" in call and SECRET in " ".join(call) for call in keychain.calls)  # never passed as an argument


def test_saved_pages_carry_no_password():
    register_secret(SECRET)
    html = (f'<form><input type="email" value="{USER}"><input name="p" type="password" class="x" value="hunter2-typed">'
            f'<input type=password value=bare><input type="text" value="kept"><p>echo {SECRET}</p></form>')
    out = scrub_html(html)
    assert "hunter2-typed" not in out and "bare" not in out and SECRET not in out
    assert 'value="kept"' in out and USER in out and out.count("[redacted]") == 3


def test_stored_sites_are_listed_by_name_only_and_can_be_removed(keychain):
    assert credentials.stored_sites() == [("careers.acme.com", USER)]
    assert not any("-w" in call or "-g" in call or "-d" in call for call in keychain.calls)  # no password was asked for
    assert credentials.delete("https://careers.acme.com/jobs") is True and credentials.stored_sites() == []
    assert credentials.delete("careers.acme.com") is False


def test_storing_asks_macos_for_the_password_not_this_program(keychain, monkeypatch):
    ran: list[list[str]] = []
    monkeypatch.setattr(credentials.subprocess, "run", lambda args, **kw: ran.append(list(args)) or subprocess.CompletedProcess(args, 0))
    assert credentials.store("https://Careers.Acme.com/login", USER) == 0
    args = ran[0]
    assert args[1:4] == ["add-generic-password", "-U", "-s"] and args[4] == credentials.SERVICE_PREFIX + "careers.acme.com"
    assert args[-1] == "-w"  # last and without a value: `security` prompts on the terminal itself
    for bad_site, user in (("not a site", USER), ("careers.acme.com", " ")):
        with pytest.raises(ValueError):
            credentials.store(bad_site, user)


def test_without_a_keychain_only_the_env_password_is_used(monkeypatch):
    from app.config import get_settings

    called: list = []
    monkeypatch.setattr(credentials, "_run", lambda *a, **k: called.append(a))
    assert credentials.keychain_available() is False  # switched off for every test (conftest)
    assert lookup("https://careers.acme.com/", default_username="me@profile.test") is None and called == []
    monkeypatch.setattr(get_settings(), "ats_account_password", OTHER)
    assert lookup("https://careers.acme.com/", default_username="me@profile.test").source == "env" and called == []


# ------------------------------------------------------------------ in the browser

pytestmark_browser = pytest.mark.browser


@pytest.fixture
async def engine(site_server, tmp_path):
    e = BrowserEngine(headless=True, user_data_dir=tmp_path / "p", screenshot_dir=tmp_path / "s", human_delays=False)
    await e.start()
    yield e
    await e.stop()


def _events(ctx) -> str:
    return "\n".join(f"{t}: {m}" for t, m in ctx.events)


async def _state(engine, *keys):
    return await engine.page.evaluate("(keys) => keys.map(k => localStorage.getItem(k))", list(keys))


async def _user_signs_in(page):
    """What you do on Google's page (the assistant never does): finish the sign-in."""
    assert identity_provider(page.url, None) or "Google Accounts" in await page.title()
    await page.evaluate("() => window.completeSignIn()")
    return GateResult.RESOLVED


@pytestmark_browser
@pytest.mark.parametrize("popup", ["", "?popup=1"], ids=["same tab", "pop-up window"])
async def test_google_sign_in_is_opened_for_you_and_the_application_continues(engine, site_server, profile, popup):
    """The site offers "Continue with Google" and no password is configured for it: Google's own page is opened,
    you finish there (the test's stand-in does), and the same application goes on - signed in - to its confirmation."""
    gate = FakeGate([_user_signs_in])
    ctx = make_ctx(engine, profile, JOB, AgentMode.AUTO, gate=gate, auto_wait=30)
    await engine.goto(f"{site_server}/careers/login/google-job.html{popup}")
    res = await apply_external(engine, engine.page, ctx)
    assert res.outcome == ApplyOutcome.APPLIED, (res.outcome, res.message, _events(ctx))
    assert [k for k, _ in gate.calls] == ["account"] and "sign in with Google" in gate.calls[0][1]
    assert gate.detectors[0] is not None  # in a real run the assistant notices by itself that you are done
    types = [t for t, _ in ctx.events]
    assert "google_sign_in" in types and "sign_in_page" in types and "signed_in" in types
    clicks, touched, signins = await _state(engine, "google_clicks", "idp_touched", "delta_signins")
    assert clicks == "1" and touched is None and signins is None  # nothing typed or pressed on Google's page
    assert len(ctx.submits) == 1


@pytestmark_browser
async def test_an_unfinished_google_sign_in_parks_the_job_and_types_nothing(engine, site_server, profile):
    """Nobody answers: the job waits as "needs you" with what to do - not applied, not skipped, nothing entered on
    Google's page, and no second or third attempt at it."""
    gate = FakeGate([])
    ctx = make_ctx(engine, profile, JOB, AgentMode.AUTO, gate=gate, auto_wait=30)
    await engine.goto(f"{site_server}/careers/login/google-job.html")
    res = await apply_external(engine, engine.page, ctx)
    assert res.outcome == ApplyOutcome.MANUAL_ACTION_REQUIRED and "sign in with Google" in res.message
    assert not res.submit_attempted and not ctx.submits and len(gate.calls) == 1
    assert "Google Accounts" in await engine.page.title()  # left open where you continue
    assert await _state(engine, "idp_touched", "google_clicks") == [None, "1"]


@pytestmark_browser
async def test_a_google_session_you_already_have_is_reused_without_asking(engine, site_server, profile):
    gate = FakeGate([])
    ctx = make_ctx(engine, profile, JOB, AgentMode.AUTO, gate=gate, auto_wait=30)
    await engine.goto(f"{site_server}/careers/login/google-job.html")
    await engine.page.evaluate("() => { localStorage.setItem('google_session', '1'); localStorage.setItem('delta_authorized', '1'); }")
    res = await apply_external(engine, engine.page, ctx)
    assert res.outcome == ApplyOutcome.APPLIED, (res.message, _events(ctx))
    assert gate.calls == [] and await _state(engine, "idp_touched") == [None]


@pytestmark_browser
async def test_a_site_session_from_before_needs_no_sign_in_at_all(engine, site_server, profile):
    ctx = make_ctx(engine, profile, JOB, AgentMode.AUTO, gate=FakeGate([]))
    await engine.goto(f"{site_server}/careers/login/google-job.html")
    await engine.page.evaluate("() => localStorage.setItem('delta_session', '1')")
    res = await apply_external(engine, engine.page, ctx)
    assert res.outcome == ApplyOutcome.APPLIED and await _state(engine, "google_clicks", "delta_signins") == [None, None]


@pytestmark_browser
@pytest.mark.parametrize("why", ["switched off", "dry run"])
async def test_google_sign_in_is_not_started_when_it_should_not_be(engine, site_server, profile, monkeypatch, why):
    from app.config import get_settings

    if why == "switched off":
        monkeypatch.setattr(get_settings(), "google_signin", False)
    mode = AgentMode.DRY_RUN if why == "dry run" else AgentMode.AUTO
    ctx = make_ctx(engine, profile, JOB, mode, gate=FakeGate([]), auto_wait=30)
    await engine.goto(f"{site_server}/careers/login/google-job.html")
    res = await apply_external(engine, engine.page, ctx)
    assert res.outcome in {ApplyOutcome.MANUAL_ACTION_REQUIRED, ApplyOutcome.DRY_RUN}
    assert await _state(engine, "google_clicks") == [None] and "Google Accounts" not in await engine.page.title()


def _stored(monkeypatch, password=SECRET, user=USER):
    register_secret(password)
    monkeypatch.setattr(navigator, "lookup_credential",
                        lambda address, default_username="": SiteCredential(normalize_site(address), user, password, "keychain"))


@pytestmark_browser
async def test_a_sign_in_stored_for_the_site_is_used_instead_of_google(engine, site_server, profile, monkeypatch):
    _stored(monkeypatch)
    ctx = make_ctx(engine, profile, JOB, AgentMode.AUTO, gate=FakeGate([]), auto_wait=30)
    await engine.goto(f"{site_server}/careers/login/google-job.html")
    await engine.page.evaluate("([u, p]) => { localStorage.setItem('delta_account', u); localStorage.setItem('delta_password', p); }", [USER, SECRET])
    res = await apply_external(engine, engine.page, ctx)
    assert res.outcome == ApplyOutcome.APPLIED, (res.message, _events(ctx))
    assert await _state(engine, "google_clicks", "delta_signins") == [None, "1"]
    log = _events(ctx)
    assert "sign-in stored for 127.0.0.1" in log and SECRET not in log and SECRET not in (res.message or "")


@pytestmark_browser
async def test_a_stored_sign_in_that_is_refused_never_creates_an_account(engine, site_server, profile, monkeypatch):
    """The stored password is wrong (changed, expired): the site's Google sign-in is offered to you next; no account
    is registered with the sign-in of an existing one, and the general password is not tried in its place."""
    from app.config import get_settings

    _stored(monkeypatch, password="-".join(["wrong", "old", "password", "1"]))
    monkeypatch.setattr(get_settings(), "ats_account_password", OTHER)
    gate = FakeGate([])
    ctx = make_ctx(engine, profile, JOB, AgentMode.AUTO, gate=gate, auto_wait=30)
    await engine.goto(f"{site_server}/careers/login/google-job.html")
    await engine.page.evaluate("([u, p]) => { localStorage.setItem('delta_account', u); localStorage.setItem('delta_password', p); }", [USER, SECRET])
    res = await apply_external(engine, engine.page, ctx)
    assert res.outcome == ApplyOutcome.MANUAL_ACTION_REQUIRED and not res.submit_attempted
    assert await _state(engine, "delta_signins", "gamma_account") == ["1", None]  # one attempt, nothing registered
    assert "account" not in [t for t, m in ctx.events if "Creating an account" in m]
    assert OTHER not in _events(ctx)


@pytestmark_browser
async def test_nothing_is_typed_into_a_sign_in_box_that_belongs_to_another_service(engine, site_server, profile, monkeypatch):
    """A password is configured, and the application leads to Google's own page: it stays empty."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "ats_account_password", OTHER)
    ctx = make_ctx(engine, profile, JOB, AgentMode.AUTO, gate=FakeGate([]), auto_wait=30)
    await engine.goto(f"{site_server}/careers/sso/job.html")
    res = await apply_external(engine, engine.page, ctx)
    assert res.outcome == ApplyOutcome.MANUAL_ACTION_REQUIRED and "sign in with Google" in res.message
    assert await engine.page.evaluate("() => [document.getElementById('identifierId').value, window.NEXT]") == ["", 0]


# ------------------------------------------------------------------ a second step after the password (OTP / MFA)

async def _user_enters_the_code(page):
    await page.evaluate("() => window.verify()")
    return GateResult.RESOLVED


async def _epsilon_account(engine, site_server):
    await engine.goto(f"{site_server}/careers/login/mfa-job.html")
    await engine.page.evaluate("([u, p]) => { localStorage.setItem('eps_account', u); localStorage.setItem('eps_password', p); }", [USER, SECRET])


@pytestmark_browser
async def test_a_verification_code_after_sign_in_waits_for_you_and_then_continues(engine, site_server, profile, monkeypatch):
    """Password by the assistant (the sign-in you stored), code by you: the run pauses at the code, types nothing
    into it, and goes on with the same application once you have entered it."""
    _stored(monkeypatch)
    gate = FakeGate([_user_enters_the_code])
    ctx = make_ctx(engine, profile, JOB, AgentMode.AUTO, gate=gate, auto_wait=30)
    await _epsilon_account(engine, site_server)
    res = await apply_external(engine, engine.page, ctx)
    assert res.outcome == ApplyOutcome.APPLIED, (res.message, _events(ctx))
    assert [k for k, _ in gate.calls] == ["otp"] and "verification code" in gate.calls[0][1]
    assert gate.detectors[0] is not None  # (in a real run the assistant sees by itself when the code page is gone)
    assert await _state(engine, "otp_touched", "eps_signins") == [None, "1"]
    assert SECRET not in _events(ctx)


@pytestmark_browser
async def test_an_unanswered_verification_code_parks_the_job_once(engine, site_server, profile, monkeypatch):
    _stored(monkeypatch)
    gate = FakeGate([])
    ctx = make_ctx(engine, profile, JOB, AgentMode.AUTO, gate=gate, auto_wait=30)
    await _epsilon_account(engine, site_server)
    res = await apply_external(engine, engine.page, ctx)
    assert res.outcome == ApplyOutcome.MANUAL_ACTION_REQUIRED and "verification code" in res.message
    assert not res.submit_attempted and len(gate.calls) == 1  # asked once: no loop on the code page
    assert await _state(engine, "otp_touched", "eps_signins") == [None, "1"]  # no code guessed, no second sign-in


def test_a_google_session_is_recognised_by_cookie_names_only():
    from app.cli import has_google_session

    assert has_google_session(["NID", "SID", "HSID"]) and has_google_session(iter(["__Secure-1PSID"]))
    assert not has_google_session(["NID", "CONSENT", "AEC"]) and not has_google_session([])
