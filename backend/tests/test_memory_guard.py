"""Memory safety: what a run costs is measured as macOS counts it, the model is not kept in memory, and a run does
not push a Mac that is out of memory further.

Background (2026-10-12): macOS showed "Your system has run out of application memory" during a run. The agent's
browser was fine (one browser, one context, one page; ~1.1 GB resident). The machine was not: the startup disk was
99% full, so macOS could not add swap beyond the 8 GB it had; other applications already used more than the RAM;
and since the local AI had been reconnected that morning, every run also kept a 3.2 GB model loaded. The resource
log had recorded resident memory only, which under-reports once macOS compresses and swaps.
"""

from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import select

from app.ai.provider import AIUnavailable, OllamaProvider
from app.browser import resources
from app.browser.resources import ResourceSample, SystemMemory, footprints, memory_state, system_memory
from app.db import session_scope
from app.engine.control import AgentControl
from app.engine.orchestrator import Orchestrator
from app.models import AgentState, ApplicationEvent

GB = 1024.0


# ------------------------------------------------------------------ the gate

@pytest.mark.parametrize("pressure,swap_used,swap_total,disk_free,state", [
    (1, 2 * GB, 8 * GB, 2.4, "ok"),  # plenty of the existing swap left
    (1, 6.5 * GB, 8 * GB, 2.4, "warn"),  # 1.5 GB of swap left and the disk cannot provide more (the incident)
    (1, 7.8 * GB, 8 * GB, 2.4, "critical"),  # nothing left
    (1, 7.8 * GB, 8 * GB, 40.0, "ok"),  # the disk has room: macOS simply adds swap files
    (1, 7.2 * GB, 8 * GB, 3.8, "warn"),  # 0.8 GB left + 0.8 GB the disk can still give: a 3 GB model does not fit
    (1, 7.2 * GB, 8 * GB, 6.0, "ok"),  # 0.8 GB left + 3 GB of disk above the reserve
    (2, 1 * GB, 8 * GB, 40.0, "warn"),  # the kernel's own warning always counts
    (4, 0.1 * GB, 1 * GB, 100.0, "critical"),
    (0, 0.0, 0.0, -1.0, "ok"),  # unknown (not macOS): never blocks
])
def test_memory_state(pressure, swap_used, swap_total, disk_free, state):
    got, why = memory_state(SystemMemory(pressure, swap_used, swap_total, disk_free))
    assert got == state
    assert (why == "") is (state == "ok")
    if state != "ok" and disk_free < 5 and swap_total:
        assert "swap" in why and "GB free" in why  # names the actual constraint: the disk


VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               59558.
Pages active:                            387903.
Pages inactive:                          381222.
Pages speculative:                         4000.
Pages wired down:                        118966.
Pages stored in compressor:              890683.
Pages occupied by compressor:             57549.
"""
TOP = """Processes: 489 total, 3 running
PID    MEM
1204   832M+
892    1.2G
55335  4056M-
77     512K
x      y
"""


def test_system_memory_is_read_from_the_kernel(monkeypatch, tmp_path):
    answers = {"kern.memorystatus_vm_pressure_level": "2",
               "vm.swapusage": "total = 8192.00M  used = 7005.12M  free = 1186.88M  (encrypted)"}
    monkeypatch.setattr(resources.sys, "platform", "darwin")
    monkeypatch.setattr(resources, "_sysctl", lambda name: answers[name])
    monkeypatch.setattr(resources, "_run", lambda cmd, timeout=10: VM_STAT if cmd[0] == "vm_stat" else "")
    monkeypatch.setattr(resources.shutil, "disk_usage", lambda _p: type("U", (), {"free": int(2.4 * 1073741824)})())
    s = system_memory(tmp_path)
    assert (s.pressure_level, s.swap_total_mb, s.swap_used_mb) == (2, 8192.0, 7005.12)
    assert round(s.disk_free_gb, 1) == 2.4 and not s.swap_can_grow and round(s.swap_left_mb) == 1187
    assert round(s.free_mb) == round((59558 + 4000) * 16384 / 1048576)
    assert round(s.compressed_mb / 1024, 1) == 13.6  # what the compressor holds, not what it occupies
    assert memory_state(s)[0] == "warn"


def test_footprint_is_resident_plus_compressed_as_top_reports_it(monkeypatch):
    monkeypatch.setattr(resources.sys, "platform", "darwin")
    monkeypatch.setattr(resources, "_run", lambda cmd, timeout=10: TOP)
    assert footprints() == {1204: 832.0, 892: 1228.8, 55335: 4056.0, 77: 0.5}


def test_missing_tools_never_break_a_run(monkeypatch):
    monkeypatch.setattr(resources.sys, "platform", "darwin")
    monkeypatch.setattr(resources, "_run", lambda cmd, timeout=10: "")
    monkeypatch.setattr(resources, "_sysctl", lambda name: "")
    s = system_memory()
    assert s.pressure_level == 0 and s.swap_total_mb == 0 and memory_state(s) == ("ok", "")
    assert footprints() == {} and resources.ai_model_mb("http://127.0.0.1:9") == 0.0 and resources.ai_model_mb("") == 0.0


def test_the_resource_line_shows_footprint_model_and_machine_state():
    s = ResourceSample(browsers=1, contexts=1, pages=1, chromium_processes=9, chromium_rss_mb=812, chromium_footprint_mb=1140,
                       python_rss_mb=64, python_footprint_mb=88, ai_model_mb=3180, listeners=6, tasks=3, memory="warn",
                       system=SystemMemory(1, 6973, 8192, 2.4))
    line = s.line(run=27, phase="after", application=3)
    for part in ("run=27", "application=3", "chromium_rss_mb=812", "chromium_footprint_mb=1140", "python_footprint_mb=88",
                 "ai_model_mb=3180", "memory=warn", "pressure=1", "swap_used_mb=6973", "swap_total_mb=8192", "disk_free_gb=2.4"):
        assert part in line, part
    assert s.as_dict()["system"]["swap_total_mb"] == 8192  # the event keeps every number


# ------------------------------------------------------------------ the model is not kept in memory

def _provider(seen: list[dict], keep_alive: str = "30s") -> OllamaProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "test-model"}]})
        seen.append({"path": request.url.path, **json.loads(request.content or b"{}")})
        return httpx.Response(200, json={"message": {"role": "assistant", "content": "ok"}})

    p = OllamaProvider(base_url="http://ollama.test", model="test-model", enabled=True, transport=httpx.MockTransport(handler))
    p.keep_alive = keep_alive
    return p


async def test_requests_ask_for_a_short_keep_alive():
    from app.config import get_settings

    assert get_settings().ollama_keep_alive == "30s"  # (Ollama's own default keeps ~3 GB loaded for 5 minutes)
    seen: list[dict] = []
    p = _provider(seen)
    assert await p.chat("system", "hello") == "ok"
    assert seen[0]["path"] == "/api/chat" and seen[0]["keep_alive"] == "30s"


async def test_unload_frees_the_model_once_and_only_if_it_was_used():
    seen: list[dict] = []
    p = _provider(seen)
    assert await p.unload() is False and seen == []  # nothing was loaded by this run: nothing is sent
    await p.chat("system", "hello")
    assert await p.unload() is True
    assert seen[-1] == {"path": "/api/generate", "model": "test-model", "keep_alive": 0}
    assert await p.unload() is False and len(seen) == 2


async def test_a_paused_model_is_unavailable_and_is_never_requested():
    seen: list[dict] = []
    p = _provider(seen)
    p.pause("the Mac is low on memory")
    assert await p.is_available() is False
    with pytest.raises(AIUnavailable, match="paused: the Mac is low on memory"):
        await p.chat("system", "hello")
    assert seen == []
    p.resume()
    assert await p.is_available() is True and await p.chat("system", "hello") == "ok"


async def test_unload_never_raises_when_the_server_is_gone():
    def boom(request):
        raise httpx.ConnectError("refused")

    p = OllamaProvider(base_url="http://ollama.test", model="m", enabled=True, transport=httpx.MockTransport(boom))
    p.loaded = True
    assert await p.unload() is False


# ------------------------------------------------------------------ the run's gate

class _Engine:
    alive = False
    user_data_dir = "/nonexistent"


class _Assistant:
    def __init__(self):
        self.provider = _provider([])


@pytest.fixture
def orch(clean_db, monkeypatch):
    from app.config import reset_settings_cache

    monkeypatch.setenv("PJA_MEMORY_GUARD", "true")  # (off for every other test: see conftest)
    reset_settings_cache()
    with session_scope() as s:
        st = AgentState.get(s)
        st.mode, st.portals, st.desired_state = "auto", ["naukri"], "RUNNING"
    o = Orchestrator(_Engine(), AgentControl(cache_s=0), assistant=_Assistant(), adapter_factory=lambda n, e: None)
    slept: list[float] = []

    async def no_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(o, "_sleep", no_sleep)
    o.slept = slept
    yield o
    monkeypatch.delenv("PJA_MEMORY_GUARD", raising=False)
    monkeypatch.setenv("PJA_MEMORY_GUARD", "false")
    reset_settings_cache()


def _states(monkeypatch, *sequence: SystemMemory):
    queue = list(sequence)
    monkeypatch.setattr(resources, "system_memory", lambda *a, **k: queue.pop(0) if len(queue) > 1 else queue[0])


def _events(kind: str) -> list[ApplicationEvent]:
    with session_scope() as s:
        return list(s.scalars(select(ApplicationEvent).where(ApplicationEvent.type == kind).order_by(ApplicationEvent.id)))


OK = SystemMemory(1, 2 * GB, 8 * GB, 40.0)
WARN = SystemMemory(1, 6.5 * GB, 8 * GB, 2.4)
CRITICAL = SystemMemory(4, 7.9 * GB, 8 * GB, 2.4)


async def test_enough_memory_changes_nothing(orch, monkeypatch):
    _states(monkeypatch, OK)
    assert await orch._memory_gate() == "" and not await orch._memory_blocked()
    assert orch.assistant.provider.paused == "" and _events("memory_warning") == [] and orch.slept == []


async def test_under_pressure_the_run_continues_without_loading_the_model(orch, monkeypatch):
    _states(monkeypatch, WARN)
    for _ in range(3):  # three applications in a row under the same condition
        assert await orch._memory_gate() == ""
    assert "low on memory" in orch.assistant.provider.paused and await orch.assistant.provider.is_available() is False
    said = _events("memory_warning")
    assert len(said) == 1 and said[0].level == "warning"  # said once, not per application
    assert "cannot add swap" in said[0].message and "without the local AI model" in said[0].message
    assert orch.memory_stop == "" and orch.slept == []  # nothing is stopped, nothing waits


async def test_when_pressure_clears_the_model_is_used_again(orch, monkeypatch):
    _states(monkeypatch, WARN, OK)
    await orch._memory_gate()
    assert orch.assistant.provider.paused
    await orch._memory_gate()
    assert orch.assistant.provider.paused == "" and len(_events("memory_ok")) == 1


async def test_out_of_memory_waits_a_bounded_time_then_stops_and_keeps_jobs_queued(orch, monkeypatch):
    monkeypatch.setenv("PJA_MEMORY_WAIT_S", "45")
    from app.config import reset_settings_cache

    reset_settings_cache()
    try:
        _states(monkeypatch, CRITICAL)
        assert await orch._memory_blocked(left=7) is True
        assert sum(orch.slept) == 45 and len(orch.slept) == 3  # waited, in steps, never forever
        assert "critical memory pressure" in orch.memory_stop
        stop = _events("memory_stop")
        assert len(stop) == 1 and stop[0].level == "error" and "7 job(s) stay queued" in stop[0].message
        assert "Close other apps or free disk space" in stop[0].message
        assert len(_events("memory_critical")) == 1
        assert await orch._memory_blocked() is True and len(_events("memory_stop")) == 1  # asked again: still one line
        assert orch.assistant.provider.paused  # and the model is not loaded meanwhile
    finally:
        monkeypatch.delenv("PJA_MEMORY_WAIT_S")
        reset_settings_cache()


async def test_out_of_memory_that_recovers_while_waiting_goes_on(orch, monkeypatch):
    _states(monkeypatch, CRITICAL, CRITICAL, OK)
    assert await orch._memory_gate() == ""
    assert orch.slept == [15, 15] and orch.memory_stop == "" and orch.assistant.provider.paused == ""


async def test_the_guard_can_be_switched_off(orch, monkeypatch):
    monkeypatch.setenv("PJA_MEMORY_GUARD", "false")
    from app.config import reset_settings_cache

    reset_settings_cache()
    try:
        _states(monkeypatch, CRITICAL)
        assert await orch._memory_gate() == "" and orch.slept == [] and orch.assistant.provider.paused == ""
    finally:
        monkeypatch.setenv("PJA_MEMORY_GUARD", "true")
        reset_settings_cache()


async def test_a_run_that_starts_out_of_memory_does_no_browser_work(orch, monkeypatch):
    """Nothing is searched, opened or applied - and the run ends normally with the reason in its log."""
    monkeypatch.setenv("PJA_MEMORY_WAIT_S", "0")
    from app.config import reset_settings_cache

    reset_settings_cache()
    started: list[str] = []

    async def start():
        started.append("browser")

    async def portal_pass(portal):
        started.append(f"search {portal}")

    orch.engine.start = start
    monkeypatch.setattr(orch, "_portal_pass", portal_pass)
    try:
        _states(monkeypatch, CRITICAL)
        summary = await orch.run()
    finally:
        monkeypatch.delenv("PJA_MEMORY_WAIT_S")
        reset_settings_cache()
    assert summary["status"] == "COMPLETED" and summary["stats"].get("memory_stop") == 1
    assert started == []  # no browser was opened, no portal was searched
    assert len(_events("memory_stop")) == 1


async def test_a_search_stops_when_memory_runs_out_in_the_middle_of_it(orch, monkeypatch):
    """Searching a portal takes dozens of result pages: it is checked before every one of them, not only before
    applications. The searches already done are kept; nothing more is opened."""
    from app.config import reset_settings_cache
    from app.profile import get_profile_store

    monkeypatch.setenv("PJA_MEMORY_WAIT_S", "0")
    reset_settings_cache()
    searched: list[str] = []

    class _Portal:
        display_name = "Naukri"

        async def ensure_logged_in(self, gate):
            return None

        async def search(self, q):
            searched.append(f"{q.keywords} / {q.location}")
            return
            yield  # (an async generator that finds nothing)

    orch.adapters["naukri"] = _Portal()
    profile = get_profile_store().load(force=True)
    orch.profile = profile.model_copy(update={"search": profile.search.model_copy(update={
        "queries": ["react developer", "frontend developer"], "locations": ["Bengaluru", "Remote"]})})
    planned = 4
    try:
        _states(monkeypatch, OK, OK, CRITICAL)
        await orch.discover_portal("naukri")
    finally:
        monkeypatch.delenv("PJA_MEMORY_WAIT_S")
        reset_settings_cache()
    assert len(searched) == 2 < planned  # two searches ran, the third was never started
    assert orch.memory_stop and len(_events("memory_stop")) == 1
    assert any("search stopped" in e.message for e in _events("limit"))


# ------------------------------------------------------------------ room for the model (run #42)

TIGHT = SystemMemory(1, 5.3 * GB, 6 * GB, 5.0)  # fine as it is (2.7 GB of headroom) - not with a 3 GB model on top


async def test_the_model_is_not_loaded_when_there_is_no_room_for_it(orch, monkeypatch):
    """Run #42 started like this, loaded the model to analyse job descriptions and made macOS add two swap files
    within minutes. The run itself goes on - by rules and saved answers - and says once why."""
    assert memory_state(TIGHT)[0] == "ok"
    monkeypatch.setattr(resources, "ai_model_mb", lambda *a, **k: 0.0)
    _states(monkeypatch, TIGHT)
    for _ in range(3):
        assert await orch._memory_gate() == ""
    assert "headroom" in orch.assistant.provider.paused and await orch.assistant.provider.is_available() is False
    said = _events("memory_warning")
    assert len(said) == 1 and "Not loading the local AI model" in said[0].message and "2.7 GB" in said[0].message
    assert orch.memory_stop == "" and orch.slept == []
    _states(monkeypatch, OK)  # memory was freed: the model is used again
    await orch._memory_gate()
    assert orch.assistant.provider.paused == ""


async def test_a_model_that_is_already_in_memory_stays_in_use(orch, monkeypatch):
    monkeypatch.setattr(resources, "ai_model_mb", lambda *a, **k: 3028.0)
    _states(monkeypatch, TIGHT)
    assert await orch._memory_gate() == ""
    assert orch.assistant.provider.paused == "" and _events("memory_warning") == []


async def test_the_room_check_can_be_switched_off(orch, monkeypatch):
    from app.config import reset_settings_cache

    monkeypatch.setenv("PJA_AI_MODEL_MEMORY_MB", "0")
    reset_settings_cache()
    monkeypatch.setattr(resources, "ai_model_mb", lambda *a, **k: 0.0)
    try:
        _states(monkeypatch, TIGHT)
        assert await orch._memory_gate() == "" and orch.assistant.provider.paused == ""
    finally:
        monkeypatch.delenv("PJA_AI_MODEL_MEMORY_MB")
        reset_settings_cache()


# ------------------------------------------------------------------ a failure capture never stalls a run (run #42)

async def test_saving_a_failed_page_never_waits_on_a_stuck_page(tmp_path, monkeypatch):
    """After a LinkedIn page timed out three times, reading the page's source for the failure record never returned:
    the run sat there for nine minutes, deaf to Stop, with the browser open."""
    import asyncio
    import time

    from playwright.async_api import Error as PlaywrightError

    from app.browser import engine as engine_module
    from app.browser.engine import BrowserEngine

    class _StuckPage:
        url = "https://www.linkedin.test/jobs/view/1/"

        def is_closed(self):
            return False

        async def screenshot(self, **kwargs):
            raise PlaywrightError("Timeout 10000ms exceeded")

        async def content(self):
            await asyncio.Event().wait()  # a navigation that never finishes

    monkeypatch.setattr(engine_module, "CAPTURE_TIMEOUT_S", 0.2)
    e = BrowserEngine(headless=True, user_data_dir=tmp_path / "p", screenshot_dir=tmp_path / "s", human_delays=False)
    started = time.monotonic()
    got = await asyncio.wait_for(e.capture_failure("job-linkedin-1", page=_StuckPage()), 5)
    assert time.monotonic() - started < 2
    assert got == {"screenshot": None, "html": None, "url": "https://www.linkedin.test/jobs/view/1/"}


def test_the_model_memory_probe_only_talks_to_a_web_address(monkeypatch):
    """The Ollama address comes from configuration: anything but http(s) (a file: path, an empty value) is never
    opened."""
    opened: list[str] = []
    monkeypatch.setattr(resources.urllib.request, "urlopen", lambda url, **kw: opened.append(url))
    assert resources.ai_model_mb("file:///etc/passwd") == 0.0 and resources.ai_model_mb("") == 0.0
    assert resources.ai_model_mb("ftp://127.0.0.1/x") == 0.0 and opened == []
