"""Job Engine: discovery -> matching -> prioritised applications -> recording.

Per portal: search every query, read and evaluate each new job (rules + local AI), then apply to the
relevant ones in priority order (best match first: React / Next.js before Angular-only, etc.).

Guarantees:
* one failing job never stops the run (each job is isolated; failures are recorded with screenshots);
* duplicates (same portal id, or same company+title applied via another portal) are skipped;
* a job is APPLIED only with a detected confirmation;
* interrupted applications are recovered on the next start (re-queued, or flagged for verification
  when a submit may already have happened); parked company-site applications continue from the page
  where they stopped instead of starting over;
* pause / resume / stop are honoured between every browser action;
* a lost internet connection pauses the run until it is back instead of failing it.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import socket
from collections import Counter
from collections.abc import Callable
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select

from app.ai.job_assistant import JobAssistant
from app.answers.answerer import Answerer
from app.answers.question_bank import Answer, QuestionContext
from app.ats.external import apply_external
from app.engine.submission import verify_page
from app.browser.engine import BrowserEngine
from app.browser.errors import LoginRequired, ManualActionRequired, NavigationFailed, StopRequested
from app.browser.forms import FormField
from app.db import session_scope
from app.engine.context import ApplyContext
from app.engine.control import AgentControl, DBHumanGate
from app.engine.recorder import (
    add_event,
    applications_since,
    find_duplicate,
    local_day_start_utc,
    memory_lookup_fn,
    record_answer,
    remember_answer,
    save_match,
    set_status,
    start_application,
    upsert_job,
)
from app.matching.matcher import JobMatcher
from app.matching.posting import Window, listing_closed, posted_of, verdict, window_to_dict
from app.matching.rules import decision_reason, title_prescreen
from app.models import (
    ActualState,
    AgentMode,
    Application,
    Decision,
    Job,
    JobStatus,
    Run,
    utcnow,
)
from app.portals.base import PortalAdapter, SearchQuery
from app.portals.types import ApplyOutcome, ApplyResult, JobPosting
from app.profile import Profile, blocking_issues, get_profile_store
from app.resumes import get_resume, select_resume

log = logging.getLogger(__name__)

AdapterFactory = Callable[[str, BrowserEngine], PortalAdapter | None]

# Statuses that mean "already handled - do not process again during discovery"
# a portal whose searches keep listing what this run has already seen is not searched further: on 2026-10-15 run 76
# made 65 Instahyre searches (5 queries x 13 cities) that listed the same few dozen jobs - 1 300 sightings, 1 new job
REDUNDANT_SEARCHES = 3

_HANDLED = {JobStatus.APPLIED, JobStatus.ALREADY_APPLIED, JobStatus.DUPLICATE, JobStatus.SKIPPED,
            JobStatus.REVIEW_PENDING, JobStatus.MANUAL_ACTION_REQUIRED, JobStatus.DRY_RUN, JobStatus.FAILED,
            JobStatus.AWAITING_APPROVAL, JobStatus.APPLYING, JobStatus.QUEUED, JobStatus.EXPIRED}

# what a portal says about a posting that is paused rather than gone
_PAUSED = re.compile(r"not\s+currently\s+accepting|temporarily\s+(?:closed|paused|unavailable)|\bpaused\b", re.I)
ANOTHER_LOOK_AFTER = timedelta(hours=12)


TRANSIENT_ATTEMPTS = 3  # how often an application that broke for a passing reason is put back in the queue


def transient_failure(exc: BaseException) -> str:
    """What broke the attempt, when it was something that passes ('' otherwise)."""
    if is_network_error(exc):
        return "the internet connection dropped"
    name, text = exc.__class__.__name__, str(exc)
    if name in {"TargetClosedError", "BrowserCrashed"} or "has been closed" in text or "Target closed" in text:
        return "the browser window was closed"
    return ""


def _settings():
    from app.config import get_settings

    return get_settings()


def _worth_another_look(job: Job) -> bool:
    """Set aside for a reason that may have passed: the listing could not be read (its page timed out while
    searching) or its portal had paused it ("Not currently accepting applications"). Neither says the job is wrong
    for you or gone for good - when a later search lists it again it is read again, at most once in twelve hours.
    Anything that was applied to, decided by you or closed for good is left as it is."""
    if job.application is not None or job.updated_at is None or utcnow() - job.updated_at < ANOTHER_LOOK_AFTER:
        return False
    reason = job.status_reason or ""
    if job.status == JobStatus.FAILED:
        return reason.startswith("Error:")  # recorded while reading the listing (never an application's failure)
    return job.status == JobStatus.EXPIRED and bool(_PAUSED.search(reason))


_NETWORK_ERRORS = ("ERR_NAME_NOT_RESOLVED", "ERR_INTERNET_DISCONNECTED", "ERR_NETWORK_CHANGED",
                   "ERR_CONNECTION_RESET", "ERR_CONNECTION_REFUSED", "ERR_ADDRESS_UNREACHABLE",
                   "ERR_NETWORK_IO_SUSPENDED", "ERR_CONNECTION_TIMED_OUT", "ERR_TIMED_OUT")


def default_adapter_factory(name: str, engine: BrowserEngine) -> PortalAdapter | None:
    from app.portals.registry import create_adapter

    return create_adapter(name, engine)


def is_network_error(exc: BaseException) -> bool:
    return any(code in str(exc) for code in _NETWORK_ERRORS)


class Orchestrator:
    def __init__(self, engine: BrowserEngine, control: AgentControl, *, assistant: JobAssistant | None = None,
                 adapter_factory: AdapterFactory = default_adapter_factory,
                 network_probe: Callable[[], bool] | None = None) -> None:
        self.engine = engine
        self.control = control
        self.assistant = assistant
        self.portal_limits: dict[str, str] = {}
        self.logged_out: dict[str, str] = {}  # portal -> why its session ended in this run (its jobs stay queued)
        self.broke_this_run: set[int] = set()  # jobs requeued after a passing failure: not tried twice in one run
        self.last_sample = None  # the latest [RESOURCE] sample
        self._memory_state = "ok"
        self.memory_stop = ""  # why applying was stopped for lack of memory ('' = it was not)  # portal -> what it said when it stopped taking applications
        self._unregistered: dict[str, list[int]] = {}  # portal -> jobs whose Apply click verifiably did nothing
        self.adapter_factory = adapter_factory
        self.adapters: dict[str, PortalAdapter] = {}
        self.stats: Counter[str] = Counter()
        self.run_id: int | None = None
        self.mode = AgentMode.DRY_RUN
        self.profile: Profile | None = None
        self.network_probe = network_probe or _internet_available
        self.max_applications: int | None = None  # per-run cap (agent --max-applications)
        # A controlled run (agent --jobs / --max-searches): exactly these queued jobs and nothing else - no search, no
        # other job of the queue, no reconsidering of skipped ones; or a search limited to its first N result pages.
        self.only_jobs: set[int] | None = None
        self.max_searches: int | None = None
        self.search_only = False  # find and evaluate only: the queue is left as it is (for reviewing a portal's yield)
        self.seen_this_run: set[tuple[str, str]] = set()  # (portal, listing id): unique listings of this run
        self.window: Window = Window()  # job recency, read from the dashboard's choice at the start of every run

    # ------------------------------------------------------------ helpers
    def _event(self, job_id: int | None, type_: str, message: str, *, level: str = "info",
               application_id: int | None = None, screenshot: str | None = None, **data: Any) -> None:
        with session_scope() as s:
            add_event(s, job_id, type_, message, level=level, application_id=application_id, run_id=self.run_id,
                      data=data, screenshot=screenshot)

    def _adapter(self, portal: str) -> PortalAdapter | None:
        if portal not in self.adapters:
            adapter = self.adapter_factory(portal, self.engine)
            if adapter is None:
                return None
            self.adapters[portal] = adapter
        return self.adapters[portal]

    def _limits_reached(self) -> str:
        p = self.profile
        assert p is not None
        cap = min(p.search.max_applications_per_run, self.max_applications or 10**6)
        if self.stats["apply_attempts"] >= cap:
            return f"run limit of {cap} applications reached"
        with session_scope() as s:
            today = applications_since(s, local_day_start_utc(0))
        if today >= p.search.max_applications_per_day:
            return f"daily limit of {p.search.max_applications_per_day} applications reached"
        return ""

    async def _sleep(self, seconds: float) -> None:
        end = asyncio.get_running_loop().time() + seconds
        while asyncio.get_running_loop().time() < end:
            await self.control.checkpoint()
            await asyncio.sleep(min(1.0, max(0.0, end - asyncio.get_running_loop().time())))

    async def _wait_for_network(self, max_minutes: float = 30) -> bool:
        """The internet dropped (DNS / connection errors): wait for it instead of failing the run."""
        if self.network_probe():
            return True
        self._event(None, "network", "No internet connection - waiting for it to come back", level="warning")
        self.control.update(message="Waiting for the internet connection")
        deadline = asyncio.get_running_loop().time() + max_minutes * 60
        while asyncio.get_running_loop().time() < deadline:
            self.control.activity("Waiting for the internet connection...")
            await self._sleep(15)
            if self.network_probe():
                self._event(None, "network", "Internet connection is back - continuing")
                return True
        return False

    # ---------------------------------------------------------------- run
    async def run(self) -> dict[str, Any]:
        store = get_profile_store()
        self.profile = store.load(force=True)
        snap = self.control.snapshot(fresh=True)
        self.mode = AgentMode(snap.mode) if snap.mode in {m.value for m in AgentMode} else AgentMode.DRY_RUN
        portals = [p for p in (snap.portals or self.profile.search.portals)]
        self.stats = Counter()
        self.logged_out = {}  # a new run tries every portal again (you may have logged in meanwhile)
        self.broke_this_run = set()
        self.seen_this_run = set()
        with session_scope() as s:
            run = Run(mode=self.mode.value, portals=portals)
            s.add(run)
            s.flush()
            self.run_id = run.id
        self.control.update(run_id=self.run_id, actual_state=ActualState.RUNNING, started_at=utcnow(),
                            message=f"Running in {self.mode.value} mode", last_error="")
        self._event(None, "run_started", f"Run #{self.run_id} started ({self.mode.value}; portals: {', '.join(portals)})")
        status, error = "COMPLETED", ""
        try:
            self.recover_interrupted()
            from app.engine.recency import current_window

            self.window = current_window()
            self._event(None, "recency", f"Job recency for this run: {self.window.label()} (chosen in the dashboard; "
                                         "older postings are not applied to)")
            if self.only_jobs is None:
                self._release_recency_skips()
            if self.only_jobs is None and _settings().reconsider_skipped:
                self._reconsider_skipped()
            await self._report_ai_status()
            if await self._memory_blocked():  # out of memory before anything started: no browser is opened at all
                portals = []
            else:
                await self.engine.start()
                await self._resources("run_start")
                if self.search_only:
                    self._event(None, "limit", "Search only: new listings are found and evaluated; nothing in the "
                                               "queue is applied to or changed in this run")
                else:
                    await self.process_queue(portals, label="left from earlier runs" if self.only_jobs is None
                                             else "chosen for this run")
            if self.only_jobs is not None:
                self._event(None, "limit", "This run was limited to the chosen job(s): no search")
                portals = []
            for portal in portals:
                await self.control.checkpoint()
                if await self._memory_blocked():
                    self._event(None, "limit", f"{portal}: no new search - the run stopped for lack of memory")
                    continue
                if self.max_applications and self._limits_reached():
                    self._event(None, "limit", f"{portal}: application cap of this run reached - no new search")
                    continue
                await self._portal_pass(portal)
        except StopRequested:
            status = "STOPPED"
            self._event(None, "run_stopped", "Run stopped by you")
        except Exception as exc:
            status, error = "ERROR", f"{exc.__class__.__name__}: {exc}"
            log.exception("Run failed")
            self.control.update(last_error=error[:2000])
            self._event(None, "run_error", error, level="error")
        finally:
            with session_scope() as s:
                run = s.get(Run, self.run_id)
                if run is not None:
                    run.status = status
                    run.error = error
                    run.stats = dict(self.stats)
                    run.finished_at = utcnow()
            summary = _summary(self.stats)
            if self.assistant is not None:  # free the model's memory now, not after its keep-alive
                try:
                    await self.assistant.provider.unload()
                except Exception as exc:  # noqa: BLE001 - never fails a run
                    log.debug("AI unload failed: %s", exc)
            if self.engine.alive:
                await self._resources("run_end", application=self.stats["apply_attempts"])
            self._event(None, "run_finished", f"Run #{self.run_id} {status.lower()}: {summary}")
            self.control.update(message=f"Last run {status.lower()}: {summary}", current_activity="",
                                current_job_id=None, current_step="", current_url="", phase="idle")
        return {"run_id": self.run_id, "status": status, "error": error, "stats": dict(self.stats)}

    def _session_lost(self, portal: str, reason: str, *, job_id: int | None = None, waiting: int = 0) -> None:
        """A portal's session ended (expired, or a security check appeared). That says nothing about any job: the
        one being applied to goes back to the queue - unless something may already have been sent, which stays with
        you to verify - and the portal's other jobs are left queued for the run that follows your next login. Said
        once per portal and run, not once per job."""
        first = portal not in self.logged_out
        self.logged_out[portal] = reason
        requeued = False
        if job_id is not None:
            with session_scope() as s:
                job = s.get(Job, job_id)
                app = job.application if job is not None else None
                if job is not None and job.status == JobStatus.MANUAL_ACTION_REQUIRED \
                        and not (app is not None and (app.submit_attempted or app.confirmed)):
                    if app is not None:
                        app.manual_action_reason = ""
                    set_status(s, job, JobStatus.QUEUED, f"Kept in the queue: {reason} - it is applied to after you "
                                                         f"log in to {portal} again")
                    add_event(s, job.id, "requeued", f"Back in the queue: {reason} (nothing was sent)", run_id=self.run_id)
                    self.stats["result_manual_action_required"] -= 1
                    self.stats["deferred"] += 1
                    requeued = True
        if first:
            self.stats["login_required"] += 1
            self._event(None, "login_required",
                        f"{portal}: {reason}. Log in again (make login-{portal}); "
                        + ("the job that was being applied to is back in the queue and " if requeued else "")
                        + f"{waiting} more queued job(s) of this portal wait for the next run. Nothing was skipped.",
                        level="warning")

    def _requeue_transient(self, job_id: int, why: str) -> None:
        """An attempt broke because the connection dropped or the browser window was closed - nothing about the job.
        Up to 2026-10-13 thirteen jobs had been recorded as Failed that way and waited for a manual Retry. The job
        goes back to the queue for a later run instead (the third such attempt is recorded as failed); never when
        something may already have been sent."""
        with session_scope() as s:
            job = s.get(Job, job_id)
            app = job.application if job is not None else None
            if job is None or job.status != JobStatus.FAILED or app is None or app.submit_attempted or app.confirmed:
                return
            if (app.attempts or 0) >= TRANSIENT_ATTEMPTS:
                set_status(s, job, JobStatus.FAILED, f"{job.status_reason} (it broke {app.attempts} times because "
                                                     f"{why}: use Retry when it is stable)"[:1000])
                return
            app.failure_reason = ""
            self.broke_this_run.add(job_id)
            set_status(s, job, JobStatus.QUEUED, f"Kept in the queue: {why} while applying (attempt {app.attempts} of "
                                                 f"{TRANSIENT_ATTEMPTS}) - nothing was sent; it is tried again on the next run")
            add_event(s, job.id, "requeued", f"Back in the queue: {why} (nothing was sent)", run_id=self.run_id)
            self.stats["result_failed"] -= 1
            self.stats["deferred"] += 1

    def _requeue_unregistered(self, portal: str, why: str) -> None:
        with session_scope() as s:
            for jid in self._unregistered.pop(portal, []):
                job = s.get(Job, jid)
                if job is None or job.status != JobStatus.MANUAL_ACTION_REQUIRED:
                    continue
                if job.application is not None:
                    job.application.manual_action_reason = ""
                set_status(s, job, JobStatus.QUEUED, f"Kept in the queue: {portal} {why} - will be applied on a later run")
                add_event(s, job.id, "requeued", f"Back in the queue: {portal} {why}", run_id=self.run_id)
                self.stats["result_manual_action_required"] -= 1
                self.stats["deferred"] += 1

    def _release_recency_skips(self) -> None:
        """Jobs skipped only for their age under a narrower window are looked at again when the window is widened."""
        from app.engine.reconsider import release_recency

        assert self.profile is not None
        try:
            moved = release_recency(self.profile, self.window, run_id=self.run_id)
        except Exception as exc:  # noqa: BLE001 - housekeeping
            log.warning("Could not look again at jobs skipped for their age: %s", exc)
            return
        changed = {k: v for k, v in moved.items() if k != "unchanged" and v}
        if changed:
            self._event(None, "reconsidered", f"Your job-recency window is now {self.window.label()}: "
                        + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in sorted(changed.items())))

    def _windows(self) -> list[Window]:
        """The run's window and the one chosen in the dashboard now: narrowing it mid-run applies at once (the
        strictest decides); widening it applies from the next run, which also looks again at jobs it had skipped."""
        from app.engine.recency import current_window

        try:
            chosen = current_window()
        except Exception:  # noqa: BLE001 - the run's own window still applies
            return [self.window]
        return [self.window] if chosen == self.window else [self.window, chosen]

    def _recency_gate(self, s: Any, job: Job) -> bool:
        """Right before a queued job is applied to: closed by its own end date, or posted outside the window ->
        it is not applied to, and says why. A posting date nobody knows waits for your decision in Auto Apply.
        Returns True when the job was set aside."""
        from app.engine.recency import strictest

        closed = listing_closed(job)
        state, said, window = ("out", f"Closed: {closed}", self.window) if closed else \
            strictest(posted_of(job), self._windows())
        if state == "in" or (state == "unknown" and self.mode != AgentMode.AUTO):
            return False
        reason = said[0].upper() + said[1:]
        if closed:
            set_status(s, job, JobStatus.EXPIRED, reason)
        elif state == "out":
            job.match_details = {**(job.match_details or {}), "recency_skip": window_to_dict(window)}
            set_status(s, job, JobStatus.SKIPPED, f"Not applied: {reason}")
            self.stats["too_old"] += 1
        else:
            set_status(s, job, JobStatus.REVIEW_PENDING, f"Not applied automatically: {reason}")
            self.stats["date_unknown"] += 1
        add_event(s, job.id, "recency", job.status_reason, run_id=self.run_id)
        return True

    def _reconsider_skipped(self) -> None:
        """Jobs skipped for a hard requirement under other preferences are evaluated again (rules only, nothing is
        opened). Never fails a run."""
        from app.engine.reconsider import reconsider_skipped

        assert self.profile is not None
        try:
            moved = reconsider_skipped(self.profile, run_id=self.run_id)
        except Exception as exc:  # noqa: BLE001 - housekeeping
            log.warning("Could not reconsider skipped jobs: %s", exc)
            return
        changed = {k: v for k, v in moved.items() if k != "unchanged" and v}
        if changed:
            self.stats["reconsidered"] = sum(changed.values())
            self._event(None, "reconsidered", "Your preferences changed since some jobs were skipped: "
                        + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in sorted(changed.items())))

    async def _report_ai_status(self) -> None:
        """One line in the run log about the local model. When it is enabled but cannot be used, that is a warning
        you see in the dashboard: written answers, unusual question wording and unknown pages then come to you."""
        if self.assistant is None:
            return
        try:
            health = await self.assistant.provider.health(use_cache=False)
        except Exception as exc:  # noqa: BLE001 - a status line never stops a run
            log.debug("AI status check failed: %s", exc)
            return
        if not health.get("enabled"):
            self._event(None, "ai_status", "Local AI is switched off (PJA_AI_ENABLED=false): only rules and your saved "
                                           "answers are used")
        elif health.get("available") and health.get("model_present"):
            self._event(None, "ai_status", f"Local AI ready: {health.get('model')} at {health.get('url')}")
        else:
            self.stats["ai_unavailable"] = 1
            self._event(None, "ai_unavailable",
                        f"Local AI is NOT available - {health.get('error') or 'unknown reason'}. Written answers (bio, "
                        "cover note, 'why us'), unusually worded questions and pages the rules do not recognise will "
                        "be left for you in this run.", level="warning")

    async def _portal_pass(self, portal: str, attempt: int = 0) -> None:
        """Discover + evaluate everything on one portal, then apply to its relevant jobs, best first."""
        try:
            await self.discover_portal(portal)
            if not self.search_only:
                await self.process_queue([portal])
        except LoginRequired as exc:
            self.stats["login_required"] += 1
            self._event(None, "login_required", f"{portal}: {exc.reason} - skipped this portal", level="warning")
        except (StopRequested, asyncio.CancelledError):
            raise
        except Exception as exc:  # a broken portal must not stop the other portals
            if is_network_error(exc) and attempt == 0 and await self._wait_for_network():
                await self._portal_pass(portal, attempt + 1)
                return
            log.exception("Portal %s failed", portal)
            self.stats["portal_errors"] += 1
            shot = await self._safe_capture(f"portal-{portal}")
            self._event(None, "portal_error", f"{portal}: {exc}", level="error", screenshot=shot)

    async def _safe_capture(self, name: str) -> str | None:
        try:
            if self.engine.alive:
                return (await asyncio.wait_for(self.engine.capture_failure(name), 30)).get("screenshot")
        except Exception:  # noqa: BLE001 - evidence only (asyncio.TimeoutError included): never fails or stalls a run
            pass
        return None

    # ------------------------------------------------------------ recovery
    def recover_interrupted(self) -> None:
        """Resume from the last known state after a crash / kill."""
        with session_scope() as s:
            for run in s.scalars(select(Run).where(Run.status == "RUNNING", Run.id != self.run_id)):
                run.status = "INTERRUPTED"
                run.finished_at = run.finished_at or utcnow()
            stuck = s.scalars(select(Job).where(Job.status.in_([JobStatus.APPLYING, JobStatus.AWAITING_APPROVAL]))).all()
            for job in stuck:
                app = job.application
                if app is not None and app.submit_attempted and not app.confirmed:
                    set_status(s, job, JobStatus.MANUAL_ACTION_REQUIRED,
                               "Interrupted after submitting - confirmation unknown, please verify on the portal")
                    app.manual_action_reason = job.status_reason
                    add_event(s, job.id, "recovered", job.status_reason, level="warning", run_id=self.run_id)
                else:
                    set_status(s, job, JobStatus.QUEUED, "Re-queued after an interrupted run")
                    add_event(s, job.id, "recovered", "Re-queued after an interrupted run", run_id=self.run_id)
                self.stats["recovered"] += 1

    # --------------------------------------------------------------- queue
    async def process_queue(self, portals: list[str], label: str = "") -> None:
        """Apply to queued jobs, highest match score first."""
        if self.memory_stop:
            return
        with session_scope() as s:
            queued = s.scalars(select(Job).where(Job.status == JobStatus.QUEUED, Job.portal.in_(portals))).all()
            if self.only_jobs is not None:
                queued = [j for j in queued if j.id in self.only_jobs]
            queued = sorted(queued, key=lambda j: (-(j.match_score or 0), j.updated_at or utcnow()))
            items = [(j.id, j.portal, _posting_from_job(j)) for j in queued]
        if not items:
            return
        blockers = [i.message for i in blocking_issues(get_profile_store().load())]
        if blockers and self.mode != AgentMode.DRY_RUN:
            self.stats["waiting_for_profile"] += len(items)
            if self.stats["profile_warned"]:
                return
            self.stats["profile_warned"] = 1
            self._event(None, "profile_incomplete",
                        f"{len(items)} relevant job(s) kept in the queue: complete config/profile.json first ("
                        + "; ".join(blockers[:4]) + "). Dry Run still works.", level="warning")
            return
        self._event(None, "queue", f"{len(items)} relevant job(s) to apply to" + (f" ({label})" if label else "")
                    + ", best match first")
        self.control.live(phase="applying")
        logged_in: set[str] = set()
        for n, (job_id, portal, posting) in enumerate(items):
            await self.control.checkpoint()
            reason = self._limits_reached()
            if reason:
                left = len(items) - n
                self.stats["deferred"] += left
                self._event(None, "limit", f"Stopping applications: {reason} ({left} job(s) kept for the next run)")
                return
            if await self._memory_blocked(left=len(items) - n):
                self.stats["deferred"] += len(items) - n
                return
            with session_scope() as s:
                job = s.get(Job, job_id)
                if job is None or job.status != JobStatus.QUEUED:
                    continue  # handled meanwhile (dashboard skip, duplicate...)
                if job_id in self.broke_this_run:
                    self.stats["deferred"] += 1  # it broke a moment ago for a passing reason: the next run tries it
                    continue
                if self._recency_gate(s, job):
                    continue  # (a queued job is never applied to past its window: it was queued days or weeks ago)
                native = job.apply_type != "external"  # applied on the portal itself (not on a company site)
                if portal in self.logged_out:
                    self.stats["deferred"] += 1  # stays queued: the portal wants you to log in again first
                    continue
                if portal in self.portal_limits and native:
                    self.stats["deferred"] += 1  # stays queued: the portal is not taking applications right now
                    continue
                dup = find_duplicate(s, job)
                if dup is not None:
                    set_status(s, job, JobStatus.DUPLICATE, f"Same role already handled via {dup.portal} (job #{dup.id})")
                    self.stats["duplicates"] += 1
                    continue
            adapter = self._adapter(portal)
            if adapter is None:
                continue
            if portal not in logged_in:
                try:
                    await adapter.ensure_logged_in(DBHumanGate(self.control))
                    logged_in.add(portal)
                except LoginRequired as exc:
                    self._session_lost(portal, exc.reason, waiting=sum(1 for _i, p, _p in items[n:] if p == portal))
                    continue
                except NavigationFailed as exc:
                    if is_network_error(exc) and await self._wait_for_network():
                        continue
                    raise
            result = await self.apply_job(job_id, adapter, posting)
            if result.transient:
                self._requeue_transient(job_id, result.transient)
            if result.login_required:
                logged_in.discard(portal)
                self._session_lost(portal, result.login_required, job_id=job_id,
                                   waiting=sum(1 for _i, p, _p in items[n + 1:] if p == portal))
                continue
            # A portal that silently stops registering its own Apply clicks (Naukri did this for hours on 2026-10-09:
            # 14 in a row, each parked as "needs you") is not a problem with those jobs: after two in a row the
            # portal's own applications wait for the next run, and both jobs go back to the queue.
            if (native and result.click_ignored and not result.portal_limit
                    and result.outcome == ApplyOutcome.MANUAL_ACTION_REQUIRED):
                self._unregistered.setdefault(portal, []).append(job_id)
                if len(self._unregistered[portal]) >= 2:
                    result.portal_limit = "it stopped registering Apply clicks (2 in a row, verified by reloading)"
                    self._requeue_unregistered(portal, result.portal_limit)
            elif native and not result.portal_limit:
                self._unregistered.pop(portal, None)  # the portal took (or properly answered) an application again
            if result.portal_limit and portal not in self.portal_limits:
                self.portal_limits[portal] = result.portal_limit
                waiting = sum(1 for _i, p, _p in items[n + 1:] if p == portal)
                self._event(None, "portal_limit",
                            f"{adapter.display_name} is not accepting more applications right now "
                            f"({result.portal_limit[:160]}). Its own applications are paused for this run - up to "
                            f"{waiting} queued job(s) stay queued for the next run; company-site applications continue.",
                            level="warning")
            delay = self.profile.search.delay_between_applications_s if self.profile else []
            if delay and max(delay) > 0 and n < len(items) - 1:
                await self._sleep(random.uniform(min(delay), max(delay)))

    # ----------------------------------------------------------- discovery
    async def discover_portal(self, portal: str) -> None:
        adapter = self._adapter(portal)
        if adapter is None:
            self._event(None, "portal_unknown", f"No adapter for portal '{portal}'", level="warning")
            return
        assert self.profile is not None
        self.control.activity(f"{adapter.display_name}: checking login")
        self.control.live(job_id=None, step=f"Checking {adapter.display_name} login", phase="discovering")
        await adapter.ensure_logged_in(DBHumanGate(self.control))
        search = self.profile.search
        years = self.profile.professional.total_experience_years if search.use_experience_filter else None
        searches = 0
        redundant = 0  # searches in a row that brought no listing new to this run
        for keywords in search.queries:
            for location in (search.locations or [""]):
                await self.control.checkpoint()
                if redundant >= REDUNDANT_SEARCHES:
                    left = len(search.queries) * len(search.locations or [""]) - searches
                    self._event(None, "limit", f"{adapter.display_name}: the last {REDUNDANT_SEARCHES} searches listed "
                                               f"only jobs already seen in this run - its {left} remaining search(es) "
                                               "are skipped (the portal shows the same listings whatever is asked)")
                    self.stats["searches_skipped_redundant"] += left
                    return
                if self.max_searches is not None and searches >= self.max_searches:
                    self._event(None, "limit", f"{adapter.display_name}: search limited to {self.max_searches} "
                                               "quer" + ("y" if self.max_searches == 1 else "ies") + " for this run")
                    return
                searches += 1
                if await self._memory_blocked():  # a long search must not push an exhausted machine further
                    self._event(None, "limit", f"{adapter.display_name}: search stopped - the Mac is out of memory")
                    return
                q = SearchQuery(keywords=keywords, location=location, experience_years=years,
                                posted_within_days=self.window.search_days(), max_pages=search.max_pages_per_query,
                                max_jobs=search.max_jobs_per_query, easy_apply_only=search.linkedin_easy_apply_only)
                label = f"{adapter.display_name}: '{keywords}'" + (f" in {location}" if location else "")
                self.control.activity(f"Searching {label}")
                self.control.live(job_id=None, step=f"Searching {label}", phase="discovering")  # no stale "current job"
                self._event(None, "search", f"Searching {label}")
                found = fresh = 0
                try:
                    async for posting in adapter.search(q):
                        found += 1
                        self.stats["discovered"] += 1
                        key = (posting.portal, posting.external_id)
                        if key in self.seen_this_run:
                            self.stats["repeat_sightings"] += 1
                        else:
                            self.seen_this_run.add(key)
                            self.stats["unique_listings"] += 1
                            fresh += 1
                        if self.stats["discovered"] % 10 == 0:
                            await self._resources("discovery", discovered=self.stats["discovered"])
                        await self.control.checkpoint()
                        try:
                            await self.handle_posting(adapter, posting, keywords)
                        except (StopRequested, LoginRequired):
                            raise
                        except Exception as exc:  # isolate each job
                            if is_network_error(exc):
                                raise
                            log.exception("Job %s failed", posting.url)
                            self.stats["job_errors"] += 1
                            shot = await self._safe_capture(f"job-{posting.portal}-{posting.external_id}")
                            self._mark_error(posting, exc, shot)
                except NavigationFailed as exc:
                    if is_network_error(exc):
                        raise
                    self._event(None, "search_failed", f"{label}: {exc}", level="error")
                self._event(None, "search_done", f"{label}: {found} job(s) found, {fresh} not already seen in this run")
                redundant = redundant + 1 if found and not fresh else 0

    def _mark_error(self, posting: JobPosting, exc: Exception, shot: str | None) -> None:
        with session_scope() as s:
            job, _ = upsert_job(s, posting, run_id=self.run_id)
            if job.status in {JobStatus.NEW, JobStatus.QUEUED, JobStatus.APPLYING}:
                set_status(s, job, JobStatus.FAILED, f"Error: {exc}"[:500])
                if job.application is not None:
                    job.application.failure_reason = str(exc)[:2000]
                    job.application.finished_at = utcnow()
            add_event(s, job.id, "error", f"{exc.__class__.__name__}: {exc}", level="error", run_id=self.run_id,
                      screenshot=shot)

    async def handle_posting(self, adapter: PortalAdapter, posting: JobPosting, query: str = "") -> None:
        """Record, de-duplicate, read and evaluate one listing. Relevant jobs are queued for the apply phase."""
        assert self.profile is not None
        with session_scope() as s:
            job, created = upsert_job(s, posting, run_id=self.run_id, query=query)
            job_id = job.id
            if created:
                self.stats["new_jobs"] += 1
            if not created and job.status in _HANDLED and not _worth_another_look(job):
                self.stats["seen_before"] += 1
                return
            if not created and job.status in _HANDLED:
                add_event(s, job.id, "another_look", f"Listed again by {adapter.display_name}: read again (it had "
                                                     f"been set aside as: {(job.status_reason or job.status)[:160]})",
                          run_id=self.run_id)
                self.stats["read_again"] += 1
            if posting.already_applied:
                if _unfinished_elsewhere(job):
                    self.stats["seen_before"] += 1
                    return  # (its badge records the click on the company-site link, not an application - see below)
                set_status(s, job, JobStatus.ALREADY_APPLIED, f"{adapter.display_name} shows this job as applied")
                self.stats["already_applied"] += 1
                return
            dup = find_duplicate(s, job)
            if dup is not None:
                set_status(s, job, JobStatus.DUPLICATE, f"Same role already handled via {dup.portal} (job #{dup.id})")
                add_event(s, job.id, "duplicate", job.status_reason, run_id=self.run_id)
                self.stats["duplicates"] += 1
                return
            early = title_prescreen(self.profile, posting.title)
            if early:
                why = "; ".join(early)
                save_match(s, job, {"score": 0.0, "decision": Decision.SKIP.value, "blockers": early,
                                    "reasons": ["Skipped from the title alone (page not opened)"], "flags": [],
                                    "category": "other"})
                set_status(s, job, JobStatus.SKIPPED, why)
                add_event(s, job.id, "evaluated", f"SKIP (title): {why}", run_id=self.run_id)
                self.stats["skipped"] += 1
                return
        self.control.activity(f"Reading: {posting.title} @ {posting.company}")
        self.control.live(job_id=job_id, step=f"Reading the job description: {posting.title} @ {posting.company}",
                          url=posting.url, phase="discovering")
        if not posting.details_loaded:
            await self.engine.fresh_page()  # one tab per job read: search results were already collected
            posting = await adapter.fetch_details(posting)
        with session_scope() as s:
            job, _ = upsert_job(s, posting, run_id=self.run_id, query=query)
            if posting.already_applied:
                if _unfinished_elsewhere(job):
                    self.stats["seen_before"] += 1
                    return
                set_status(s, job, JobStatus.ALREADY_APPLIED, f"{adapter.display_name} shows this job as applied")
                self.stats["already_applied"] += 1
                return
            if posting.extra.get("expired"):
                said = str(posting.extra.get("expired_reason") or "").strip()
                set_status(s, job, JobStatus.EXPIRED, f"{adapter.display_name}: {said[:160]}" if said else
                           "The job is closed / no longer accepting applications")
                add_event(s, job.id, "expired", job.status_reason, run_id=self.run_id)
                self.stats["expired"] += 1
                return
            closed = listing_closed(job)
            age_state, age_said = ("out", "") if closed else verdict(posted_of(job), self.window)
            if closed:
                set_status(s, job, JobStatus.EXPIRED, f"Closed: {closed}")
                add_event(s, job.id, "expired", job.status_reason, run_id=self.run_id)
                self.stats["expired"] += 1
                return
            if age_state == "out":  # too old for your window: not even evaluated (no page time, no model)
                job.match_details = {**(job.match_details or {}), "recency_skip": window_to_dict(self.window)}
                set_status(s, job, JobStatus.SKIPPED, age_said[0].upper() + age_said[1:])
                add_event(s, job.id, "recency", job.status_reason, run_id=self.run_id)
                self.stats["too_old"] += 1
                return
        self.control.live(step=f"Evaluating fit: {posting.title} @ {posting.company}")
        matcher = JobMatcher(self.profile, self.assistant)
        result = await matcher.evaluate(posting.to_job_input())
        decision = Decision(result.decision)
        why = decision_reason(result, self.profile.matching.apply_threshold)
        if age_state == "unknown" and decision == Decision.APPLY:
            decision, why = Decision.MANUAL_REVIEW, f"Relevant (score {result.score}) - but {age_said}"
        with session_scope() as s:
            job = s.get(Job, job_id)
            assert job is not None
            save_match(s, job, result.to_dict())
            add_event(s, job.id, "evaluated", f"{decision.value} (score {result.score}): {why}"[:1000],
                      run_id=self.run_id, data={"score": result.score, "decision": decision.value,
                                                "resume": result.resume_id, "priority": result.priority})
            if decision == Decision.SKIP:
                set_status(s, job, JobStatus.SKIPPED, why or "Not relevant")
                self.stats["skipped"] += 1
                return
            if decision == Decision.MANUAL_REVIEW:
                set_status(s, job, JobStatus.REVIEW_PENDING, why or "Needs your review")
                self.stats["review_pending"] += 1
                return
            set_status(s, job, JobStatus.QUEUED, f"Relevant (score {result.score})"
                       + (f" - {result.priority}" if result.priority else ""))
            self.stats["relevant"] += 1

    # --------------------------------------------------------- application
    async def verify_submission(self, job_id: int, app_id: int, adapter: PortalAdapter,
                                posting: JobPosting) -> ApplyResult:
        """'Retry verification': look again for evidence that an earlier submit went through. Nothing is filled or
        submitted - the portal's own 'Applied' state, then the page the submit ended on."""
        self.stats["apply_attempts"] -= 1
        self.stats["verifications"] += 1
        with session_scope() as s:
            app = s.get(Application, app_id)
            final_url = (app.final_url or "") if app is not None else ""
            external = app is not None and app.apply_target == "external"
            add_event(s, job_id, "verify_started", "Re-checking whether the earlier submission went through "
                                                    "(nothing is submitted again)", application_id=app_id,
                      run_id=self.run_id)
        self.control.live(job_id=job_id, step=f"Verifying the submission: {posting.title} @ {posting.company}",
                          url=final_url or posting.url, phase="applying")
        text, evidence = "", []
        try:
            if not external:
                await self.engine.fresh_page()
                details = await adapter.fetch_details(posting)
                if details.already_applied:
                    text = f"{adapter.display_name} now shows this job as applied"
                    evidence.append(f"portal_status: {text}")
            if not text and final_url and final_url.startswith("http"):  # (a separate tab, closed by verify_page)
                text, evidence = await verify_page(self.engine, final_url)
        except Exception as exc:  # noqa: BLE001 - verification never breaks the run
            log.warning("verification of job %s failed: %s", job_id, exc)
            evidence.append(f"verification error: {exc}"[:200])
        with session_scope() as s:
            job = s.get(Job, job_id)
            app = s.get(Application, app_id)
            assert job is not None and app is not None
            app.verify_requested = False
            app.verification_checked_at = utcnow()
            app.verification_evidence = (list(app.verification_evidence or []) + evidence)[-12:]
            if text:
                app.verification_state, app.confirmed, app.applied_at = "confirmed", True, utcnow()
                app.confirmation_text = f"Verified later: {text}"[:2000]
                set_status(s, job, JobStatus.APPLIED, f"Verified: {text}"[:1000])
                add_event(s, job.id, "result_applied", f"Verified: {text}", application_id=app_id, run_id=self.run_id)
                self.stats["verified_applied"] += 1
                return ApplyResult(ApplyOutcome.APPLIED, f"Verified: {text}", confirmation_text=text,
                                   verification="confirmed", evidence=evidence, submit_attempted=True)
            app.verification_state = "uncertain"
            reason = ("Submitted - confirmation not verified (re-checked: still no evidence on "
                      + (final_url[:120] or adapter.display_name) + ")")
            app.manual_action_reason = reason
            set_status(s, job, JobStatus.MANUAL_ACTION_REQUIRED, reason[:1000])
            add_event(s, job.id, "result_manual_action_required", reason, level="warning", application_id=app_id,
                      run_id=self.run_id)
            self.stats["verification_uncertain"] += 1
        return ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED, reason, verification="uncertain", evidence=evidence,
                           submit_attempted=True)

    async def apply_job(self, job_id: int, adapter: PortalAdapter, posting: JobPosting) -> ApplyResult:
        profile = get_profile_store().load()
        self.profile = profile
        snap = self.control.snapshot(fresh=True)  # a mode change from the dashboard applies to the next job
        if snap.mode in {m.value for m in AgentMode}:
            self.mode = AgentMode(snap.mode)
        self.stats["apply_attempts"] += 1
        with session_scope() as s:
            job = s.get(Job, job_id)
            assert job is not None
            details = job.match_details or {}
            category = details.get("resume_category") or details.get("category", "frontend")
            resume = get_resume(profile, job.resume_id) or select_resume(profile, category, job.skills or [])
            if resume is not None and not resume.absolute_path.is_file():
                resume = select_resume(profile, category, job.skills or [])
            previous = job.application
            if previous is not None and previous.verify_requested:
                verify_only = True
            else:
                verify_only = False
            resume_url = ""
            if (previous is not None and previous.resume_url and previous.apply_target == "external"
                    and not previous.submit_attempted):
                resume_url = previous.resume_url
            elif _unfinished_elsewhere(job):
                # An application that moved to a company site and was not sent continues there. Going back through the
                # portal would end it: once its company-site link has been opened, the portal shows the job as
                # "Applied" - which says nothing about the application on the other site.
                resume_url = previous.final_url
            if verify_only:
                app_id = previous.id
            else:
                app = start_application(s, job, self.mode, resume_id=resume.id if resume else None,
                                        resume_path=str(resume.absolute_path) if resume else None)
                app_id = app.id
        if verify_only:
            return await self.verify_submission(job_id, app_id, adapter, posting)
        with session_scope() as s:
            job = s.get(Job, job_id)
            assert job is not None
            add_event(s, job.id, "apply_started", f"Applying ({self.mode.value}) with resume "
                                                  f"'{resume.id if resume else 'none'}'"
                      + (" - continuing on the company site where it stopped" if resume_url else ""),
                      application_id=app_id, run_id=self.run_id)
        self.control.activity(f"Applying: {posting.title} @ {posting.company}")
        self.control.live(job_id=job_id, step=f"Opening the application: {posting.title} @ {posting.company}",
                          url=resume_url or posting.url, phase="applying")

        async def log_event(type_: str, message: str, level: str = "info", data: dict | None = None) -> None:
            self._event(job_id, type_, message, level=level, application_id=app_id, **(data or {}))

        async def on_answer(_f: FormField | None, q: QuestionContext, a: Answer) -> None:
            with session_scope() as s:
                record_answer(s, app_id, q, a)

        async def on_submit() -> None:
            with session_scope() as s:
                a = s.get(Application, app_id)
                if a is not None:
                    a.submit_attempted = True
                    a.last_step = "submit_clicked"
                add_event(s, job_id, "submit", "Submitting application", application_id=app_id, run_id=self.run_id)

        async def on_step(description: str, url: str) -> None:
            self.control.live(step=description, url=url or None)
            with session_scope() as s:
                a = s.get(Application, app_id)
                if a is not None:
                    a.steps = (a.steps or 0) + 1
                    a.last_step = description[:128]

        async def remember(question: str, answer: str) -> None:
            with session_scope() as s:
                remember_answer(s, question, answer, company=posting.company, portal=posting.portal,
                                title=posting.title)
            self._event(job_id, "answer_saved", f"Saved your answer for future forms: {question[:120]}",
                        application_id=app_id)

        blockers = [i.message for i in blocking_issues(profile)]
        ctx = ApplyContext(
            engine=self.engine, profile=profile,
            answerer=Answerer(profile, self.assistant, memory_lookup=memory_lookup_fn()),
            mode=self.mode, job=posting, resume_path=resume.absolute_path if resume else None,
            gate=DBHumanGate(self.control, job_id, app_id), log_event=log_event, record_answer=on_answer,
            on_submit_attempt=on_submit, submit_blockers=blockers, on_step=on_step, remember=remember,
            recency_check=self._final_recency_check(job_id),
        )
        await self.engine.fresh_page()  # application-scoped tab: closed (with its renderer) at the next boundary
        main_page = self.engine.page
        external_page = None
        try:
            if resume_url:
                result = ApplyResult(ApplyOutcome.EXTERNAL, "Continue the parked application",
                                     external_url=resume_url)
            elif blockers and self.mode != AgentMode.DRY_RUN:
                # name / email / phone / resume missing: never start filling forms with gaps
                await ctx.event("blocked", "Profile incomplete - not applying: " + "; ".join(blockers), level="warning")
                result = ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED,
                                     "Profile incomplete - complete config/profile.json first: " + "; ".join(blockers))
            else:
                await self._resources("before", application=self.stats["apply_attempts"], job=job_id)
                result = await adapter.apply(posting, ctx)
            if result.outcome == ApplyOutcome.EXTERNAL:
                with session_scope() as s:
                    a = s.get(Application, app_id)
                    if a is not None:
                        a.apply_target = "external"
                        a.last_step = "external"
                    j = s.get(Job, job_id)
                    if j is not None:
                        j.apply_type = "external"
                        j.external_url = j.external_url or result.external_url
                self._event(job_id, "external", f"Application continues on {result.external_url}",
                            application_id=app_id)
                external_page = result.external_page
                if external_page is None and result.external_url:
                    external_page = await self.engine.new_page()
                    await self.engine.goto(result.external_url, page=external_page)
                if external_page is None:
                    result = ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED, "External application link missing")
                else:
                    self.control.live(url=external_page.url)
                    result = await apply_external(self.engine, external_page, ctx)
        except StopRequested:
            self._finish_interrupted(job_id, app_id)
            raise
        except LoginRequired as exc:
            result = ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED, exc.reason, login_required=exc.reason)
        except ManualActionRequired as exc:
            result = ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED, exc.reason)
        except Exception as exc:
            log.exception("Application failed for job %s", job_id)
            shot = await self._safe_capture(f"apply-{job_id}")
            result = ApplyResult(ApplyOutcome.FAILED, f"{exc.__class__.__name__}: {exc}", screenshot=shot,
                                 transient=transient_failure(exc))
        finally:
            for p in list(self.engine.context.pages) if self.engine.alive else []:
                if p is not main_page and not p.is_closed():
                    try:
                        await p.close()  # external tabs: their result (and resume point) is recorded
                    except Exception:
                        pass
            try:
                if not main_page.is_closed():
                    self.engine.use_page(main_page)
                elif self.engine.alive:  # the site closed our tab: the run continues with exactly one live tab
                    await self.engine.ensure_alive()
                    self._event(job_id, "tab_closed_by_site", "The page closed its own tab - a new tab was opened",
                                level="warning")
            except Exception:
                pass
        if (result.outcome == ApplyOutcome.MANUAL_ACTION_REQUIRED and result.verification == "uncertain"
                and result.submit_attempted and external_page is None and not resume_url):
            result = await self._portal_recheck(job_id, app_id, adapter, posting, result)
        self._finalize(job_id, app_id, result)
        if ctx.recency_blocked and not result.submit_attempted:
            with session_scope() as s:  # stopped before the click for its age: a skip with its reason, not "needs you"
                job = s.get(Job, job_id)
                if job is not None and job.status in {JobStatus.MANUAL_ACTION_REQUIRED, JobStatus.DRY_RUN}:
                    job.match_details = {**(job.match_details or {}), "recency_skip": window_to_dict(self.window)}
                    set_status(s, job, JobStatus.SKIPPED, f"Not applied: {ctx.recency_blocked}")
                    if job.application is not None:
                        job.application.manual_action_reason = ""
        await self._resources("after", application=self.stats["apply_attempts"], job=job_id)
        return result

    def _final_recency_check(self, job_id: int):
        """The check inside the central submit decision: the page's own dates when it shows them, else the recorded
        ones. Posted outside the window, or closed -> never sent; unknown -> never sent in Auto Apply."""
        from app.engine.recency import final_check, snapshot

        with session_scope() as s:
            job = s.get(Job, job_id)
            record = snapshot(job) if job is not None else None

        async def check(page) -> str:
            if record is None:
                return ""
            return await final_check(record, self._windows(), page, unknown_blocks=self.mode == AgentMode.AUTO,
                                     on_pass=lambda said: self._event(job_id, "recency_check",
                                                                      f"Last check before submitting: {said}"),
                                     on_page_dates=keep_page_dates)

        def keep_page_dates(found) -> None:
            """The portal's own dates, read on the job page: kept on the record (they beat a listing's label)."""
            from app.engine.recorder import record_posted

            with session_scope() as s:
                job = s.get(Job, job_id)
                if job is not None:
                    record_posted(job, SimpleNamespace(posted_at=found.posted_at, posted_date_only=found.posted_date_only,
                                                       valid_through=found.valid_through, posted_text=""))

        return check

    async def _portal_recheck(self, job_id: int, app_id: int, adapter: PortalAdapter, posting: JobPosting,
                              result: ApplyResult) -> ApplyResult:
        """A submit on the portal itself ended without a readable confirmation (the dialog closed with the tab, the
        renderer crashed...): the portal's own job page is its record. Read once - nothing is filled or submitted."""
        try:
            await self.engine.fresh_page()
            details = await adapter.fetch_details(posting)
        except Exception as exc:  # noqa: BLE001 - a re-check never breaks the run
            log.info("portal re-check of job %s failed: %s", job_id, exc)
            return result
        if not details.already_applied:
            result.evidence = list(result.evidence or []) + [f"portal_recheck: {adapter.display_name} does not show "
                                                             "the job as applied"]
            return result
        text = f"{adapter.display_name} shows this job as applied (job page re-read after the submit)"
        self._event(job_id, "verified", text, application_id=app_id)
        return ApplyResult(ApplyOutcome.APPLIED, text, confirmation_text=text, submit_attempted=True,
                           verification="confirmed", evidence=list(result.evidence or []) + [f"portal_status: {text}"])

    async def _resources(self, phase: str, **tags):
        """[RESOURCE] sample (pages, Chromium processes / memory footprint, the AI model, listeners, tasks, and the
        machine's own memory state) - see browser/resources.py."""
        from app.browser.resources import log_sample

        def record(sample, t):
            with session_scope() as s:
                add_event(s, t.get("job"), "resource", sample.line(**t), run_id=self.run_id, data={**sample.as_dict(), **t})

        ai_url = self.assistant.provider.base_url if self.assistant is not None and self.assistant.provider.enabled else ""
        sample = await log_sample(self.engine, record=record, ai_url=ai_url, run=self.run_id, phase=phase, **tags)
        self.last_sample = sample or self.last_sample
        return sample

    async def _memory_blocked(self, left: int = 0) -> bool:
        """True when the run must not start more browser work for lack of memory (said once in the run log)."""
        if self.memory_stop:
            return True
        short = await self._memory_gate()
        if not short:
            return False
        self.memory_stop = short
        self.stats["memory_stop"] = 1
        kept = f" {left} job(s) stay queued for the next run." if left else ""
        self._event(None, "memory_stop", f"Stopped applying: the Mac is out of memory ({short}).{kept} Close other apps "
                    "or free disk space, then start again.", level="error")
        return True

    async def _memory_gate(self) -> str:
        """Before an application: is the machine able to take it? '' = go on.

        * memory under pressure -> go on without the local AI (its model needs ~3 GB) and say so once;
        * memory exhausted -> wait (bounded) for it to recover, then stop applying: the reason is returned and the
          remaining jobs stay queued. Nothing is killed: only you decide what to close."""
        from app.browser.resources import memory_state, system_memory

        from app.config import get_settings

        settings = get_settings()
        if not settings.memory_guard:
            return ""
        provider = self.assistant.provider if self.assistant is not None else None
        waited = 0.0
        while True:
            memory = await asyncio.to_thread(system_memory)
            state, why = memory_state(memory)
            if state != self._memory_state:
                self._memory_state = state
                if state == "warn":
                    self._event(None, "memory_warning", f"Low on memory: {why}. Continuing without the local AI model "
                                "(it needs about 3 GB) until this clears - written answers and unknown pages are left "
                                "for you meanwhile.", level="warning")
                elif state == "ok" and self.stats["memory_warnings"]:
                    self._event(None, "memory_ok", "Memory pressure has cleared - the local AI is used again")
                if state != "ok":
                    self.stats["memory_warnings"] += 1
            if provider is not None:
                no_room = await self._no_room_for_model(memory, provider) if state == "ok" else ""
                if state == "ok" and not no_room:
                    provider.resume()
                else:
                    provider.pause(f"the Mac is low on memory ({why or no_room})")
                    if state != "ok":
                        await provider.unload()
            if state != "critical":
                return ""
            if waited == 0:
                released = await self._release_browser_memory()
                self._event(None, "memory_critical", f"Out of memory: {why}. Waiting up to {settings.memory_wait_s} s "
                            "for it to recover before the next application" + (
                                " - the agent's open page was released (sign-ins are kept); nothing of yours is closed"
                                if released else " (nothing of yours is closed)") + ".", level="warning")
                self.control.live(step=f"Waiting for memory: {why}"[:200])
            if waited >= settings.memory_wait_s:
                return why
            await self._sleep(15)
            waited += 15

    async def _release_browser_memory(self) -> bool:
        """Between two units of work, give back what the agent's own browser holds: the open page (with its embedded
        frames - a LinkedIn page runs in ~10 renderer processes, ~1.5-2 GB) is replaced by a blank tab. The browser,
        the profile and every sign-in stay; nothing is in the middle of an application here (the gate runs only
        between applications and searches)."""
        if not getattr(self.engine, "alive", False):
            return False
        try:
            await self.engine.fresh_page()
            return True
        except Exception as exc:  # noqa: BLE001 - releasing memory is best effort
            log.debug("releasing the browser page failed: %s", exc)
            return False

    async def _no_room_for_model(self, memory: Any, provider: Any) -> str:
        """'' when the local model may be used; otherwise why not. Loading it takes about 3 GB at once: with less than
        that (plus the warning margin) left before the Mac runs out, the load itself causes the shortage - run #42
        started with 2.9 GB of headroom, loaded the model for job analysis and pushed macOS into adding two swap
        files within minutes. A model that is already in memory costs nothing more and stays in use."""
        from app.browser.resources import SWAP_HEADROOM_WARN_MB, ai_model_mb

        from app.config import get_settings

        need = get_settings().ai_model_memory_mb
        if need <= 0 or memory.swap_headroom_mb >= SWAP_HEADROOM_WARN_MB + need:
            return ""
        if await asyncio.to_thread(ai_model_mb, getattr(provider, "base_url", "")) > 0:
            return ""
        why = (f"only {memory.swap_headroom_mb / 1024:.1f} GB of memory headroom left - loading the local AI model "
               f"needs about {need / 1024:.1f} GB")
        if not self.stats["ai_no_room"]:
            self.stats["ai_no_room"] = 1
            self._event(None, "memory_warning", f"Not loading the local AI model: {why}. Rules and your saved answers "
                        "are used; written answers and unknown pages are left for you until memory is freed.",
                        level="warning")
        return why

    def _finish_interrupted(self, job_id: int, app_id: int) -> None:
        with session_scope() as s:
            job = s.get(Job, job_id)
            app = s.get(Application, app_id)
            if job is None or app is None:
                return
            if app.submit_attempted and not app.confirmed:
                set_status(s, job, JobStatus.MANUAL_ACTION_REQUIRED, "Stopped right after submitting - please verify")
            else:
                set_status(s, job, JobStatus.QUEUED, "Stopped before submitting - will resume next run")
            app.finished_at = utcnow()
            add_event(s, job.id, "interrupted", job.status_reason, application_id=app_id, run_id=self.run_id)

    def _finalize(self, job_id: int, app_id: int, result: ApplyResult) -> None:
        mapping = {
            ApplyOutcome.APPLIED: JobStatus.APPLIED,
            ApplyOutcome.DRY_RUN: JobStatus.DRY_RUN,
            ApplyOutcome.MANUAL_ACTION_REQUIRED: JobStatus.MANUAL_ACTION_REQUIRED,
            ApplyOutcome.FAILED: JobStatus.FAILED,
            ApplyOutcome.ALREADY_APPLIED: JobStatus.ALREADY_APPLIED,
            ApplyOutcome.REJECTED: JobStatus.SKIPPED,
            ApplyOutcome.EXTERNAL: JobStatus.MANUAL_ACTION_REQUIRED,
            ApplyOutcome.EXPIRED: JobStatus.EXPIRED,
        }
        status = mapping[result.outcome]
        if result.portal_limit and status == JobStatus.MANUAL_ACTION_REQUIRED and not result.submit_attempted:
            status = JobStatus.QUEUED
            result.message = (f"Kept in the queue: the portal is not accepting more applications right now "
                              f"({result.portal_limit[:200]}) - will be applied on a later run")
        if status == JobStatus.APPLIED and (not result.confirmation_text or result.verification in {"uncertain", "failed"}):
            status = JobStatus.MANUAL_ACTION_REQUIRED  # never claim success without genuine evidence
            result.message = "Submitted - confirmation not verified (no confirmation captured)"
            result.verification = "uncertain"
        with session_scope() as s:
            job = s.get(Job, job_id)
            app = s.get(Application, app_id)
            assert job is not None and app is not None
            app.finished_at = utcnow()
            app.ats_platform = result.ats or app.ats_platform
            app.final_url = result.external_url or app.final_url or job.url
            app.submit_attempted = (app.submit_attempted or result.submit_attempted) and not result.nothing_sent
            app.last_step = result.outcome.value.lower()
            app.resume_url = (result.resume_url or "") if status == JobStatus.MANUAL_ACTION_REQUIRED else ""
            if result.verification or status == JobStatus.APPLIED:
                app.verification_state = result.verification or "confirmed"
                app.verification_evidence = list(result.evidence)[:12]
                app.verification_checked_at = utcnow()
            reason = result.message
            if status == JobStatus.APPLIED:
                app.confirmed = True
                app.confirmation_text = result.confirmation_text[:2000]
                app.applied_at = utcnow()
            elif status == JobStatus.MANUAL_ACTION_REQUIRED:
                app.manual_action_reason = (result.message + ("\nUnresolved: " + "; ".join(result.unresolved)
                                                              if result.unresolved else ""))[:4000]
            elif status == JobStatus.FAILED:
                app.failure_reason = result.message[:4000]
            elif result.outcome == ApplyOutcome.REJECTED:
                reason = "Rejected by you in review mode"
            set_status(s, job, status, reason[:1000])
            level = {"FAILED": "error", "MANUAL_ACTION_REQUIRED": "warning"}.get(status.value, "info")
            add_event(s, job.id, f"result_{status.value.lower()}", reason or status.value, level=level,
                      application_id=app_id, run_id=self.run_id, screenshot=result.screenshot,
                      data={"confirmation": result.confirmation_text, "ats": result.ats,
                            "unresolved": result.unresolved, "url": result.external_url})
        self.stats[f"result_{status.value.lower()}"] += 1
        self.control.live(step=f"{status.value.replace('_', ' ').title()}: {result.message[:200]}")
        log.info("Job %s -> %s (%s)", job_id, status.value, result.message)


def _unfinished_elsewhere(job: Job) -> bool:
    """Our own application for this job moved to another site (a company career page / ATS) and nothing was sent
    there. The portal's "Applied" badge is then no evidence: it appears as soon as the company-site link is opened."""
    from urllib.parse import urlparse

    from app.security import is_web_url

    app = job.application
    if app is None or app.confirmed or app.submit_attempted:
        return False
    if app.apply_target != "external" and job.apply_type != "external":
        return False  # (apply_target is reset when an attempt starts; the job itself says where it is applied to)
    if not is_web_url(app.final_url):
        return False
    there, here = urlparse(app.final_url).netloc.lower(), urlparse(job.url or "").netloc.lower()
    return bool(there) and there != here and not there.endswith("." + here.removeprefix("www."))


_SUMMARY_PARTS = [
    ("result_applied", "applied"), ("result_dry_run", "dry-run"), ("result_manual_action_required", "need you"),
    ("result_failed", "failed"), ("relevant", "relevant"), ("review_pending", "to decide"), ("skipped", "skipped"),
    ("already_applied", "already applied"), ("duplicates", "duplicates"), ("expired", "expired"),
    ("result_expired", "closed"), ("deferred", "deferred by limits"), ("new_jobs", "new jobs"),
    ("login_required", "portal(s) needing login"), ("job_errors", "job errors"), ("recovered", "recovered"),
    ("waiting_for_profile", "waiting for your profile"),
]


def _summary(stats: Counter[str]) -> str:
    parts = [f"{stats[k]} {label}" for k, label in _SUMMARY_PARTS if stats.get(k)]
    if stats.get("discovered"):
        parts.append(f"{stats['discovered']} listings scanned")
    return ", ".join(parts) or "nothing to do"


def _internet_available() -> bool:
    for host in ("www.google.com", "www.naukri.com", "1.1.1.1"):
        try:
            socket.create_connection((host, 443), timeout=4).close()
            return True
        except OSError:
            continue
    return False


def _posting_from_job(job: Job) -> JobPosting:
    return JobPosting(portal=job.portal, external_id=job.external_id, url=job.url, title=job.title,
                      company=job.company, location=job.location, experience_text=job.experience_text,
                      salary_text=job.salary_text, posted_text=job.posted_text, description=job.description,
                      skills=list(job.skills or []), apply_type=job.apply_type, external_url=job.external_url,
                      details_loaded=bool(job.description))
