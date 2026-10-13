"""LinkedIn sign-in detection against a mock of the *current* LinkedIn (tests/fixtures/sites/linkedin-new):
React sign-in page with generated ids, /checkpoint/challenge security check, the /flagship-web/login?vcd=
post-verification hop, member navigation without the legacy global-nav classes, guest pages, expired sessions."""

from __future__ import annotations

import asyncio

import pytest

from app.browser.engine import BrowserEngine
from app.browser.errors import LoginRequired
from app.cli import wait_for_linkedin_login
from app.portals.linkedin import LinkedInAdapter
from app.portals.linkedin_session import LinkedInState, classify_page, guidance

pytestmark = pytest.mark.browser


@pytest.fixture
async def engine(site_server, tmp_path):
    e = BrowserEngine(headless=True, user_data_dir=tmp_path / "p", screenshot_dir=tmp_path / "s", human_delays=False)
    await e.start()
    yield e
    await e.stop()


@pytest.fixture
def li(engine, site_server):
    return LinkedInAdapter(engine, f"{site_server}/linkedin-new")


async def _session_cookie(engine, site_server, name="li_at"):
    await engine.context.add_cookies([{"name": name, "value": "AQEDAS-test", "url": site_server}])


async def _complete_login(page, base: str, *, stay: bool = False) -> None:
    """What you do in the window: sign in, then pass LinkedIn's security check."""
    await page.goto(f"{base}/login/" + ("?stay=1" if stay else ""))
    await page.fill("input[type=email]", "me@example.com")
    await page.fill("input[type=password]", "x")
    await page.click("#go")
    await page.wait_for_url("**/checkpoint/challenge/**")
    await page.wait_for_timeout(2500)  # you read the code from your email...
    await page.fill("input[name=pin]", "123456")
    await page.click("#verify")


# ------------------------------------------------------------------ page states

async def test_login_page(li, engine, site_server):
    await engine.goto(f"{site_server}/linkedin-new/login/")
    check = await li.login_state(navigate=False)
    assert check.state == LinkedInState.LOGIN_PAGE and not check.authenticated
    assert "password field visible" in check.signals and "sign-in URL" in check.signals


async def test_logged_out_guest_state(li, engine, site_server):
    await engine.goto(f"{site_server}/linkedin-new/")
    check = await li.login_state(navigate=False)
    assert check.state == LinkedInState.LOGGED_OUT and not check.session_cookie
    assert not await li.is_logged_in()  # /feed/ redirects to sign-in


async def test_authenticated_session_without_legacy_nav_classes(li, engine, site_server):
    await _session_cookie(engine, site_server)
    check = await li.login_state(navigate=True)
    assert check.authenticated, check.describe()
    assert "member navigation (My Network / Messaging / Me)" in check.signals
    assert "authenticated session cookie" in check.signals
    assert await engine.page.locator(".global-nav__me").count() == 0  # the old detector's only signal is gone
    assert await li.is_logged_in()


async def test_security_verification_is_never_success(li, engine, site_server):
    await _session_cookie(engine, site_server)  # even with a session cookie present
    await engine.goto(f"{site_server}/linkedin-new/checkpoint/challenge/")
    check = await li.login_state(navigate=False)
    assert check.state == LinkedInState.VERIFICATION and not check.authenticated
    with pytest.raises(LoginRequired, match="checkpoint"):
        li._check_session()


async def test_expired_session(li, engine, site_server):
    await _session_cookie(engine, site_server, name="li_rm")  # remembered member, session cookie gone
    check = await li.login_state(navigate=True)
    assert check.state == LinkedInState.EXPIRED and not check.authenticated
    assert "/login" in check.url
    with pytest.raises(LoginRequired):
        await li.ensure_logged_in(None)


# ------------------------------------------------------------------ waiting while you sign in

async def test_post_login_redirect_is_detected_and_resumes_automatically(li, engine, site_server):
    base = f"{site_server}/linkedin-new"
    await engine.goto(f"{base}/login/")
    lines: list[str] = []
    waiter = asyncio.create_task(wait_for_linkedin_login(li, engine, 40, poll_s=0.3, out=lines.append))
    await _complete_login(engine.page, base)  # sign-in -> security check -> /flagship-web/login?vcd -> /feed/
    ok, check, history = await waiter
    assert ok, (check.describe(), history)
    assert history[0] == "LOGIN_PAGE" and "VERIFICATION" in history and history[-1] == "AUTHENTICATED"
    assert engine.page.url.endswith("/feed/")
    assert any("security check" in ln for ln in lines)


async def test_session_on_a_sign_in_url_is_confirmed_in_a_background_tab(li, engine, site_server):
    """LinkedIn sometimes leaves you on /flagship-web/login after verification although the session exists."""
    base = f"{site_server}/linkedin-new"
    await _complete_login(engine.page, base, stay=True)
    await engine.page.wait_for_url("**/flagship-web/login/**")
    tabs_before = len(engine.context.pages)
    ok, check, _history = await wait_for_linkedin_login(li, engine, 15, poll_s=0.3, out=lambda _m: None)
    assert ok and "checked /feed/ in a background tab" in check.signals
    assert "/flagship-web/login" in engine.page.url  # your tab was not navigated
    assert len(engine.context.pages) == tabs_before  # the background tab was closed


async def test_login_completed_in_another_tab(li, engine, site_server):
    base = f"{site_server}/linkedin-new"
    await engine.goto(f"{base}/login/")
    other = await engine.context.new_page()
    waiter = asyncio.create_task(wait_for_linkedin_login(li, engine, 40, poll_s=0.3, out=lambda _m: None))
    await _complete_login(other, base)
    ok, check, _ = await waiter
    assert ok and check.url.endswith("/feed/")


async def test_timeout_reports_what_was_detected(li, engine, site_server):
    base = f"{site_server}/linkedin-new"
    await engine.goto(f"{base}/login/")
    await engine.page.fill("input[type=email]", "me@example.com")
    await engine.page.click("#go")  # stuck on the security check
    await engine.page.wait_for_url("**/checkpoint/challenge/**")
    ok, check, history = await wait_for_linkedin_login(li, engine, 2, poll_s=0.3, out=lambda _m: None)
    assert not ok and check.state == LinkedInState.VERIFICATION
    assert "checkpoint/challenge" in check.describe() and "no authenticated session cookie" in check.describe()
    assert "security check" in guidance(check, history)


# ------------------------------------------------------------------ pure classification

@pytest.mark.parametrize("url,dom,cookie,remembered,expected", [
    ("https://www.linkedin.com/login/", {"password": True, "signInHeading": True}, False, False, "LOGIN_PAGE"),
    ("https://www.linkedin.com/flagship-web/login/?vcd=Ag", {"password": True}, False, False, "LOGIN_PAGE"),
    ("https://www.linkedin.com/uas/login?session_redirect=x", {"password": True}, False, False, "LOGIN_PAGE"),
    ("https://www.linkedin.com/login/?session_redirect=x", {"password": True}, False, True, "EXPIRED"),
    ("https://www.linkedin.com/login/", {"password": True, "expiredText": True}, False, False, "EXPIRED"),
    ("https://www.linkedin.com/checkpoint/challenge/AQGZ", {"challengeText": True}, False, False, "VERIFICATION"),
    ("https://www.linkedin.com/checkpoint/challenge/AQGZ", {}, True, False, "VERIFICATION"),
    ("https://www.linkedin.com/checkpoint/lg/login-submit", {"password": True}, False, False, "LOGIN_PAGE"),
    ("https://www.linkedin.com/feed/", {"meMenu": True, "navLinks": 4}, True, False, "AUTHENTICATED"),
    ("https://www.linkedin.com/feed/?trk=x", {}, True, False, "AUTHENTICATED"),  # members-only URL + cookie
    ("https://www.linkedin.com/check/add-phone", {}, True, False, "AUTHENTICATED"),  # post-sign-in prompt
    ("https://www.linkedin.com/feed/", {"legacyNav": True, "navLinks": 2}, False, False, "AUTHENTICATED"),
    ("https://www.linkedin.com/jobs/", {"guest": True, "password": True}, False, False, "LOGIN_PAGE"),
    ("https://www.linkedin.com/", {"guest": True}, False, False, "LOGGED_OUT"),
    ("https://www.linkedin.com/", {"guest": True}, False, True, "EXPIRED"),
    ("https://www.linkedin.com/feed/", {"meMenu": True, "navLinks": 4, "password": True}, True, False, "LOGIN_PAGE"),
    ("https://www.linkedin.com/some/page", {}, False, False, "UNKNOWN"),
])
def test_classify_page(url, dom, cookie, remembered, expected):
    assert classify_page(url, dom, session_cookie=cookie, remembered=remembered).state.value == expected


def test_guidance_after_verification_loop():
    from app.portals.linkedin_session import LoginCheck

    check = LoginCheck(LinkedInState.LOGIN_PAGE, "www.linkedin.com/flagship-web/login/", ["sign-in URL"], False)
    hint = guidance(check, ["LOGIN_PAGE", "VERIFICATION", "LOGIN_PAGE"])
    assert "did not create a session" in hint and "PJA_BROWSER_CHANNEL=chrome" in hint


# ------------------------------------------------------------------ the `make login-linkedin` command itself

def _headless_cli(monkeypatch, site_server, tmp_path):
    import app.browser.engine as engine_mod
    from app.config import get_settings

    real = engine_mod.BrowserEngine

    class HeadlessEngine(real):  # the command opens a visible window; tests run headless
        def __init__(self, *a, **kw):
            kw["headless"] = True
            kw.setdefault("user_data_dir", tmp_path / "cli-profile")
            super().__init__(*a, **kw)

    monkeypatch.setattr(engine_mod, "BrowserEngine", HeadlessEngine)
    monkeypatch.setattr(get_settings(), "linkedin_base_url", f"{site_server}/linkedin-new")


def test_login_command_timeout_explains_the_state(monkeypatch, site_server, tmp_path, capsys):
    import argparse

    from app.cli import cmd_login

    _headless_cli(monkeypatch, site_server, tmp_path)
    code = cmd_login(argparse.Namespace(portal="linkedin", timeout=3))
    out = capsys.readouterr().out
    assert code == 1
    assert "LinkedIn sign-in was not detected within 3 s" in out
    assert "Last state seen : LOGIN_PAGE at" in out and "/linkedin-new/login/" in out
    assert "no authenticated session cookie (li_at)" in out and "What to do" in out
    assert "Timed out waiting for login." not in out  # the old, unhelpful message


def test_login_command_detects_existing_session(monkeypatch, site_server, tmp_path, capsys):
    import argparse

    from app.cli import cmd_login

    _headless_cli(monkeypatch, site_server, tmp_path)

    async def seed():  # a session saved by an earlier sign-in in the same persistent profile
        from app.browser.engine import BrowserEngine

        e = BrowserEngine(human_delays=False)
        await e.start()
        await e.context.add_cookies([{"name": "li_at", "value": "AQEDAS-test", "url": site_server,
                                      "expires": 4102444800}])
        await e.stop()

    asyncio.run(seed())
    assert cmd_login(argparse.Namespace(portal="linkedin", timeout=5)) == 0
    assert "Already logged in to LinkedIn" in capsys.readouterr().out
