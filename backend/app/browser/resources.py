"""Resource instrumentation for long runs: is anything accumulating across applications - and is the machine able
to take the next one?

One line per sample (log + DB event ``resource``):

    [RESOURCE] run=13 application=12 phase=after browsers=1 contexts=1 pages=1 chromium_processes=9
               chromium_rss_mb=812 chromium_footprint_mb=1140 renderer_max_mb=301 python_rss_mb=64
               python_footprint_mb=88 ai_model_mb=3180 listeners=6 tasks=7
               memory=ok pressure=1 swap_used_mb=6973 swap_total_mb=8192 disk_free_gb=2.4 urls=[...]

Chromium processes are found by this engine's ``--user-data-dir`` (the agent's own profile), so your everyday
Chrome is never counted. Two sizes are recorded per process group:

* ``rss`` - resident pages, from ``ps``;
* ``footprint`` - resident **plus compressed** pages, from ``top`` (what macOS shows in Force Quit / Activity
  Monitor). Under memory pressure macOS compresses and swaps, and RSS alone under-reports what a process costs.

``system_memory`` adds what decides whether the machine is in trouble: the kernel's own pressure level, swap in use,
and free disk space - macOS can only add swap while the startup disk has room, and "Your system has run out of
application memory" is what it says when it cannot.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# the gate (see memory_state): the kernel's pressure levels, and how little swap / disk is "cannot grow any more"
PRESSURE_NORMAL, PRESSURE_WARN, PRESSURE_CRITICAL = 1, 2, 4
DISK_RESERVE_GB = 3.0  # macOS stops creating swap files around here: only disk space above it can become swap
SWAP_HEADROOM_WARN_MB = 2048  # with less room than this to swap into, a ~3 GB model no longer fits
SWAP_HEADROOM_CRITICAL_MB = 300


@dataclass
class SystemMemory:
    pressure_level: int = 0  # 1 normal, 2 warning, 4 critical (0: unknown / not macOS)
    swap_used_mb: float = 0.0
    swap_total_mb: float = 0.0
    disk_free_gb: float = -1.0  # of the volume that holds the swap files (-1: unknown)
    free_mb: float = 0.0  # pages nobody uses right now
    compressed_mb: float = 0.0  # what the compressor is holding (uncompressed size)

    @property
    def swap_can_grow(self) -> bool:
        return self.disk_free_gb < 0 or self.disk_free_gb >= DISK_RESERVE_GB

    @property
    def swap_left_mb(self) -> float:
        return max(self.swap_total_mb - self.swap_used_mb, 0.0)

    @property
    def swap_headroom_mb(self) -> float:
        """How much more can still be swapped out: what is left in the existing swap files plus the disk space macOS
        can turn into new ones. Unknown disk (not macOS / unreadable) = no limit known."""
        if self.disk_free_gb < 0 or not self.swap_total_mb:
            return float("inf")
        return self.swap_left_mb + max(self.disk_free_gb - DISK_RESERVE_GB, 0.0) * 1024


@dataclass
class ResourceSample:
    browsers: int = 0
    contexts: int = 0
    pages: int = 0
    urls: list[str] = field(default_factory=list)
    chromium_processes: int = 0
    chromium_rss_mb: float = 0.0
    chromium_footprint_mb: float = 0.0
    renderer_max_mb: float = 0.0
    python_rss_mb: float = 0.0
    python_footprint_mb: float = 0.0
    ai_model_mb: float = 0.0  # the model the local AI server holds in memory right now (0: none loaded)
    listeners: int = 0
    tasks: int = 0
    pids: list[str] = field(default_factory=list)  # "pid:type" of the agent's own processes
    memory: str = ""  # ok | warn | critical (see memory_state)
    memory_reason: str = ""
    system: SystemMemory = field(default_factory=SystemMemory)

    def line(self, **tags) -> str:
        head = " ".join(f"{k}={v}" for k, v in tags.items() if v not in (None, ""))
        s = self.system
        return (f"[RESOURCE] {head} browsers={self.browsers} contexts={self.contexts} pages={self.pages} "
                f"chromium_processes={self.chromium_processes} chromium_rss_mb={self.chromium_rss_mb:.0f} "
                f"chromium_footprint_mb={self.chromium_footprint_mb:.0f} "
                f"renderer_max_mb={self.renderer_max_mb:.0f} python_rss_mb={self.python_rss_mb:.0f} "
                f"python_footprint_mb={self.python_footprint_mb:.0f} ai_model_mb={self.ai_model_mb:.0f} "
                f"listeners={self.listeners} tasks={self.tasks} memory={self.memory or 'unknown'} "
                f"pressure={s.pressure_level} swap_used_mb={s.swap_used_mb:.0f} swap_total_mb={s.swap_total_mb:.0f} "
                f"disk_free_gb={s.disk_free_gb:.1f} urls={[u[:60] for u in self.urls]}")

    def as_dict(self) -> dict:
        return asdict(self)


def _run(cmd: list[str], timeout: float = 10) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _ps() -> list[tuple[int, int, int, str]]:
    rows = []
    for line in _run(["ps", "-axo", "pid=,ppid=,rss=,command="]).splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) == 4 and parts[0].isdigit():
            rows.append((int(parts[0]), int(parts[1]), int(parts[2]), parts[3]))
    return rows


_SIZE = re.compile(r"^(\d+(?:\.\d+)?)([BKMG])[+-]?$")


def _mb(text: str) -> float:
    m = _SIZE.match(text or "")
    return float(m.group(1)) * {"B": 1 / 1048576, "K": 1 / 1024, "M": 1.0, "G": 1024.0}[m.group(2)] if m else 0.0


def footprints() -> dict[int, float]:
    """pid -> memory footprint in MB (resident + compressed), as ``top`` reports it. {} where ``top`` cannot say."""
    if sys.platform != "darwin":
        return {}
    out: dict[int, float] = {}
    for line in _run(["top", "-l", "1", "-stats", "pid,mem", "-n", "5000"], timeout=20).splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit():
            out[int(parts[0])] = _mb(parts[1])
    return out


def chromium_processes(user_data_dir: str) -> list[tuple[int, int, str]]:
    """(pid, rss_kb, command) of every Chromium process using this profile directory."""
    key = f"--user-data-dir={user_data_dir}"
    return [(pid, rss, cmd) for pid, _ppid, rss, cmd in _ps() if key in cmd]


def python_rss_mb() -> float:
    for pid, _ppid, rss, _cmd in _ps():
        if pid == os.getpid():
            return rss / 1024
    return 0.0


def _sysctl(name: str) -> str:
    return _run(["sysctl", "-n", name], timeout=5).strip()


def system_memory(volume: str | Path = "/System/Volumes/Data") -> SystemMemory:
    """The machine's memory state (macOS). Unknown values stay 0 / -1 and never raise."""
    s = SystemMemory()
    if sys.platform != "darwin":
        return s
    level = _sysctl("kern.memorystatus_vm_pressure_level")
    s.pressure_level = int(level) if level.isdigit() else 0
    swap = _sysctl("vm.swapusage")
    total, used = re.search(r"total = ([\d.]+)M", swap), re.search(r"used = ([\d.]+)M", swap)
    s.swap_total_mb = float(total.group(1)) if total else 0.0
    s.swap_used_mb = float(used.group(1)) if used else 0.0
    stat = _run(["vm_stat"], timeout=5)
    page = re.search(r"page size of (\d+) bytes", stat)
    size = int(page.group(1)) if page else 16384

    def pages(label: str) -> float:
        m = re.search(rf"{label}:\s+(\d+)\.", stat)
        return int(m.group(1)) * size / 1048576 if m else 0.0

    s.free_mb = pages("Pages free") + pages("Pages speculative")
    s.compressed_mb = pages("Pages stored in compressor")
    try:
        where = Path(volume) if Path(volume).exists() else Path.home()
        s.disk_free_gb = shutil.disk_usage(where).free / 1073741824
    except OSError:
        s.disk_free_gb = -1.0
    return s


def memory_state(s: SystemMemory) -> tuple[str, str]:
    """('ok' | 'warn' | 'critical', why). The kernel's own pressure level decides; swap that is nearly used up and
    cannot grow (the disk is full) is a warning before the kernel raises one, and critical when it is exhausted."""
    full_disk = (f"; the startup disk has only {s.disk_free_gb:.1f} GB free, so macOS cannot add swap"
                 if not s.swap_can_grow else
                 f"; the startup disk has only {s.disk_free_gb:.1f} GB free, so macOS can add very little swap"
                 if s.swap_headroom_mb < SWAP_HEADROOM_WARN_MB else "")
    swap = f"swap {s.swap_used_mb / 1024:.1f} of {s.swap_total_mb / 1024:.1f} GB used"
    if s.pressure_level >= PRESSURE_CRITICAL:
        return "critical", f"macOS reports critical memory pressure ({swap}{full_disk})"
    if s.swap_headroom_mb < SWAP_HEADROOM_CRITICAL_MB:
        return "critical", f"{swap}{full_disk}"
    if s.pressure_level >= PRESSURE_WARN:
        return "warn", f"macOS reports memory pressure ({swap}{full_disk})"
    if s.swap_headroom_mb < SWAP_HEADROOM_WARN_MB:
        return "warn", f"{swap}{full_disk}"
    return "ok", ""


def ai_model_mb(base_url: str, timeout: float = 2.0) -> float:
    """Memory the local AI server (Ollama) holds for loaded models right now, in MB. 0 when none / unreachable."""
    if not base_url.lower().startswith(("http://", "https://")):  # the configured Ollama address, nothing else
        return 0.0
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/api/ps", timeout=timeout) as r:  # noqa: S310  # nosec B310
            data = json.load(r)
        return sum(float(m.get("size") or 0) for m in data.get("models") or []) / 1048576
    except (OSError, ValueError):
        return 0.0


def _listeners(obj) -> int:
    impl = getattr(obj, "_impl_obj", obj)
    events = getattr(impl, "_events", None) or {}
    try:
        return sum(len(v) for v in events.values())
    except TypeError:
        return 0


def _process_kind(cmd: str) -> str:
    m = re.search(r"--type=([\w-]+)", cmd)
    return m.group(1) if m else "browser"


async def sample(engine, *, ai_url: str = "", with_footprint: bool = True) -> ResourceSample:
    s = ResourceSample()
    ctx = getattr(engine, "_context", None)
    alive = bool(ctx is not None and engine.alive)
    s.contexts = 1 if alive else 0
    s.browsers = 1 if alive else 0  # one Chromium per engine (persistent context or launched browser)
    if alive:
        pages = list(ctx.pages)
        s.pages = len(pages)
        s.urls = [p.url for p in pages]
        s.listeners = sum(_listeners(p) for p in pages) + _listeners(ctx)
    procs = await asyncio.to_thread(chromium_processes, str(engine.user_data_dir))
    s.chromium_processes = len(procs)
    s.chromium_rss_mb = sum(r for _p, r, _c in procs) / 1024
    renderers = [r for _p, r, c in procs if "--type=renderer" in c]
    s.renderer_max_mb = (max(renderers) / 1024) if renderers else 0.0
    s.python_rss_mb = await asyncio.to_thread(python_rss_mb)
    s.pids = [f"{os.getpid()}:python", *[f"{p}:{_process_kind(c)}" for p, _r, c in procs]]
    if with_footprint:
        mem = await asyncio.to_thread(footprints)
        s.chromium_footprint_mb = sum(mem.get(p, 0.0) for p, _r, _c in procs)
        s.python_footprint_mb = mem.get(os.getpid(), 0.0)
    s.system = await asyncio.to_thread(system_memory)
    s.memory, s.memory_reason = memory_state(s.system)
    if ai_url:
        s.ai_model_mb = await asyncio.to_thread(ai_model_mb, ai_url)
    s.tasks = len(asyncio.all_tasks())
    return s


async def log_sample(engine, *, record=None, ai_url: str = "", **tags) -> ResourceSample | None:
    """Sample, log one [RESOURCE] line and (optionally) record it; never raises."""
    try:
        s = await sample(engine, ai_url=ai_url)
    except Exception as exc:  # noqa: BLE001 - instrumentation must never break a run
        log.debug("resource sample failed: %s", exc)
        return None
    log.info(s.line(**tags))
    if record is not None:
        try:
            record(s, tags)
        except Exception as exc:  # noqa: BLE001
            log.debug("resource record failed: %s", exc)
    return s
