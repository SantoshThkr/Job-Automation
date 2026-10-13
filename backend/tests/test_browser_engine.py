"""Phase 3: browser engine against local mock pages with a real headless Chromium."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.answers.answerer import Answerer
from app.browser.engine import BrowserEngine
from app.browser.errors import ElementNotFound, NavigationFailed, StopRequested
from app.browser.forms import FormFiller, collect_errors, scan_fields
from app.browser.selectors import T
from app.matching.analyzer import JobInput

pytestmark = pytest.mark.browser


class FakeControl:
    def __init__(self) -> None:
        self.state = "RUNNING"
        self.paused_ticks = 0

    async def checkpoint(self) -> None:
        while self.state == "PAUSED":
            self.paused_ticks += 1
            await asyncio.sleep(0.05)
        if self.state == "STOPPED":
            raise StopRequested()


@pytest.fixture
async def control():
    return FakeControl()


@pytest.fixture
async def engine(site_server, tmp_path, control):
    e = BrowserEngine(headless=True, user_data_dir=tmp_path / "profile", screenshot_dir=tmp_path / "shots",
                      human_delays=False, checkpoint=control.checkpoint)
    await e.start()
    yield e
    await e.stop()


async def test_navigation_click_type_and_robust_targets(engine, site_server):
    await engine.goto(f"{site_server}/engine/index.html")
    # first two strategies do not match; the visible-text strategy does
    apply = T("apply button", css=["#does-not-exist", "button.old-apply-class"], text=["Apply now"])
    await engine.click(apply)
    assert await engine.text_of(T(css="#out")) == "clicked"
    await engine.type(T(label="Your name"), "Asha")
    assert await engine.page.locator("#name").input_value() == "Asha"


async def test_wait_for_delayed_element_and_timeouts(engine, site_server):
    await engine.goto(f"{site_server}/engine/index.html")
    await engine.click(T(css="#delayed-trigger"))
    el = await engine.wait_for(T(css="#late"), timeout_ms=3000)
    assert await el.inner_text() == "I am late"
    with pytest.raises(ElementNotFound):
        await engine.wait_for(T(css="#never"), timeout_ms=300)
    key, _ = await engine.wait_for_any({"a": T(css="#nope"), "b": T(css="#heading")}, timeout_ms=1000)
    assert key == "b"


async def test_page_change_detection(engine, site_server):
    await engine.goto(f"{site_server}/engine/index.html")
    before = await engine.page_signature()
    assert not await engine.wait_for_page_change(before, timeout_ms=300)
    await engine.click(T(css="#spa"))
    assert await engine.wait_for_page_change(before, timeout_ms=2000)
    before = await engine.page_signature()
    await engine.click(T(css="#nav-link"))
    assert await engine.wait_for_page_change(before, timeout_ms=3000)
    assert await engine.exists(T(css="#p2"), timeout_ms=2000)


async def test_infinite_scroll(engine, site_server):
    await engine.goto(f"{site_server}/engine/index.html")
    count = await engine.scroll_to_load_all(T(css=".item"), max_rounds=12)
    assert count == 45


async def test_click_through_temporary_overlay(engine, site_server):
    await engine.goto(f"{site_server}/engine/index.html")
    await engine.click(T(css="#show-overlay"))
    await engine.click(T(css="#real-btn"))  # retried until the overlay disappears / forced
    assert await engine.text_of(T(css="#out")) == "clicked"


async def test_popup_tab(engine, site_server):
    await engine.goto(f"{site_server}/engine/index.html")
    new_page = await engine.expect_popup(lambda: engine.click(T(css="#popup-link")))
    assert new_page is not None and new_page.url.endswith("page2.html")
    await new_page.close()


async def test_navigation_failure_is_reported(engine, site_server):
    await engine.page.route("**/broken.html", lambda route: route.abort("internetdisconnected"))
    with pytest.raises(NavigationFailed):
        await engine.goto(f"{site_server}/engine/broken.html", retries=1)


async def test_navigation_timeout_is_retried_then_fails(engine, site_server):
    engine.nav_timeout = 500

    async def hang(route):
        await asyncio.sleep(2)
        try:
            await route.abort()
        except Exception:
            pass

    await engine.page.route("**/slow.html", hang)
    with pytest.raises(NavigationFailed):
        await engine.goto(f"{site_server}/engine/slow.html", retries=1)


async def test_failure_capture_writes_files(engine, site_server, tmp_path):
    await engine.goto(f"{site_server}/engine/index.html")
    info = await engine.capture_failure("unit test")
    assert info["screenshot"] and Path(info["screenshot"]).is_file()
    assert info["html"] and "Engine test" in Path(info["html"]).read_text()


async def test_pause_resume_and_stop(engine, site_server, control):
    await engine.goto(f"{site_server}/engine/index.html")
    control.state = "PAUSED"

    async def resume_later():
        await asyncio.sleep(0.4)
        control.state = "RUNNING"

    asyncio.create_task(resume_later())
    await engine.click(T(css="#real-btn"))  # blocks while paused, then continues
    assert control.paused_ticks > 3
    assert await engine.text_of(T(css="#out")) == "clicked"
    control.state = "STOPPED"
    with pytest.raises(StopRequested):
        await engine.click(T(css="#real-btn"))


async def test_crash_recovery_restarts_browser(engine, site_server):
    await engine.goto(f"{site_server}/engine/index.html")
    await engine.context.close()  # simulate a browser crash / user closing the window
    assert not engine.alive
    await engine.goto(f"{site_server}/engine/page2.html")  # goto calls ensure_alive
    assert engine.alive and engine.restarts == 1
    assert await engine.exists(T(css="#p2"), timeout_ms=2000)


async def test_closed_tab_recovery(engine, site_server):
    await engine.goto(f"{site_server}/engine/index.html")
    await engine.page.close()
    await engine.goto(f"{site_server}/engine/page2.html")
    assert await engine.exists(T(css="#p2"), timeout_ms=2000)


async def test_persistent_session_survives_restart(site_server, tmp_path):
    profile_dir = tmp_path / "persist"
    e1 = BrowserEngine(headless=True, user_data_dir=profile_dir, screenshot_dir=tmp_path, human_delays=False)
    await e1.start()
    await e1.goto(f"{site_server}/engine/index.html")
    await e1.page.evaluate("() => localStorage.setItem('session', 'logged-in')")
    await e1.stop()
    e2 = BrowserEngine(headless=True, user_data_dir=profile_dir, screenshot_dir=tmp_path, human_delays=False)
    await e2.start()
    await e2.goto(f"{site_server}/engine/index.html")
    assert await e2.page.evaluate("() => localStorage.getItem('session')") == "logged-in"
    await e2.stop()


async def test_blocker_detection(engine, site_server):
    await engine.goto(f"{site_server}/engine/blockers.html")
    assert await engine.detect_blockers() == []
    await engine.click(T(text="captcha"))
    assert "captcha" in await engine.detect_blockers()
    await engine.click(T(text="otp"))
    assert "otp" in await engine.detect_blockers()


async def test_form_scanner_understands_labels(engine, site_server):
    await engine.goto(f"{site_server}/engine/form.html")
    fields = {f.label: f for f in await scan_fields(engine.page)}
    labels = set(fields)
    assert {"First Name *", "Last Name", "Email", "Phone number", "Notice period",
            "Are you willing to relocate?", "Why do you want to join us?", "City", "Resume/CV",
            "I agree to the privacy policy", "GitHub profile"} <= labels, labels
    assert "Hidden" not in labels
    assert fields["First Name *"].required
    assert fields["Are you willing to relocate?"].type == "radio"
    assert fields["Are you willing to relocate?"].option_labels == ["Yes", "No"]
    assert fields["Notice period"].type == "select" and fields["Notice period"].is_empty
    assert fields["City"].value == "Pune"


async def test_form_filler_fills_from_profile(engine, site_server, profile):
    await engine.goto(f"{site_server}/engine/form.html")
    job = JobInput(title="Frontend Engineer", company="Acme", description="React")
    recorded = []
    filler = FormFiller(engine, Answerer(profile, None), resume_path=profile.resumes[0].absolute_path, job=job,
                        portal="mock", on_answer=lambda f, q, a: recorded.append((q.question, a.value)))
    report = await filler.fill(engine.page)
    page = engine.page
    assert await page.locator("#fn").input_value() == "Asha"
    assert await page.locator("input[name=last_name]").input_value() == "Verma"
    assert await page.locator("input[name=email]").input_value() == "asha.verma.dev@gmail.com"
    assert await page.locator("#exp").input_value() == "6"
    assert await page.locator("#notice").input_value() == "30"
    assert await page.locator("input[name=relocate][value=no]").is_checked()
    assert await page.locator("input[name=consent]").is_checked()
    assert await page.locator("input[name=github]").input_value() == "https://github.com/ashaverma"
    assert await page.locator("#prefilled").input_value() == "Pune"  # prefilled values are kept
    assert report.resume_uploaded
    assert await page.locator("#resume").evaluate("el => el.files.length") == 1
    # the open-ended question is written from the verified profile + job (no AI needed), filled and verified
    why = next(i for i in report.items if "Why do you want" in i.question)
    assert why.status == "filled" and why.verified and why.answer.mode == "composed"
    assert "React" in why.answer.value and "Acme" in why.answer.value
    assert ("First Name", "Asha") in recorded


async def test_collect_errors(engine, site_server):
    await engine.goto(f"{site_server}/engine/form.html")
    await engine.page.evaluate("""() => { const d = document.createElement('div'); d.className = 'error-message';
        d.textContent = 'This field is required'; document.body.appendChild(d); }""")
    assert "This field is required" in await collect_errors(engine.page)
