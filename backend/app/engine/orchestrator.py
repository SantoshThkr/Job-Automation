"""Job Engine: discovery -> matching -> application -> recording, one job at a time.

Guarantees:
* one failing job never stops the run (each job is isolated; failures are recorded with screenshots);
* duplicates (same portal id, or same company+title applied via another portal) are skipped;
* a job is APPLIED only with a detected confirmation;
* interrupted applications are recovered on the next start (re-queued, or flagged for verification
  when a submit may already have happened);
* pause / resume / stop are honoured between every browser action.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections import Counter
from collections.abc import Callable
from typing import Any

from sqlalchemy import select

from app.ai.job_assistant import JobAssistant
from app.answers.answerer import Answerer
from app.answers.question_bank import Answer, QuestionContext
from app.ats.external import apply_external
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
    save_match,
    set_status,
    start_application,
    upsert_job,
)
from app.matching.matcher import JobMatcher
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
_HANDLED = {JobStatus.APPLIED, JobStatus.ALREADY_APPLIED, JobStatus.DUPLICATE, JobStatus.SKIPPED,
            JobStatus.REVIEW_PENDING, JobStatus.MANUAL_ACTION_REQUIRED, JobStatus.DRY_RUN, JobStatus.FAILED,
            JobStatus.AWAITING_APPROVAL, JobStatus.APPLYING}


def default_adapter_factory(name: str, engine: BrowserEngine) -> PortalAdapter | None:
    from app.portals.registry import create_adapter

    return create_adapter(name, engine)


class Orchestrator:
    def __init__(self, engine: BrowserEngine, control: AgentControl, *, assistant: JobAssistant | None = None,
                 adapter_factory: AdapterFactory = default_adapter_factory) -> None:
        self.engine = engine
        self.control = control
        self.assistant = assistant
        self.adapter_factory = adapter_factory
        self.adapters: dict[str, PortalAdapter] = {}
        self.stats: Counter[str] = Counter()
        self.run_id: int | None = None
        self.mode = AgentMode.DRY_RUN
        self.profile: Profile | None = None

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
        if self.stats["apply_attempts"] >= p.search.max_applications_per_run:
            return f"run limit of {p.search.max_applications_per_run} applications reached"
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

    # ---------------------------------------------------------------- run
    async def run(self) -> dict[str, Any]:
        store = get_profile_store()
        self.profile = store.load(force=True)
        snap = self.control.snapshot(fresh=True)
        self.mode = AgentMode(snap.mode) if snap.mode in {m.value for m in AgentMode} else AgentMode.DRY_RUN
        portals = [p for p in (snap.portals or self.profile.search.portals)]
        self.stats = Counter()
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
            await self.engine.start()
            await self.process_queue(portals)
            for portal in portals:
                await self.control.checkpoint()
                try:
                    await self.discover_portal(portal)
                except LoginRequired as exc:
                    self.stats["login_required"] += 1
                    self._event(None, "login_required", f"{portal}: {exc.reason} - skipped this portal", level="warning")
                except (StopRequested, asyncio.CancelledError):
                    raise
                except Exception as exc:  # a broken portal must not stop the other portals
                    log.exception("Portal %s failed", portal)
                    self.stats["portal_errors"] += 1
                    shot = await self._safe_capture(f"portal-{portal}")
                    self._event(None, "portal_error", f"{portal}: {exc}", level="error", screenshot=shot)
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
            self._event(None, "run_finished", f"Run #{self.run_id} {status.lower()}: {summary}")
            self.control.update(message=f"Last run {status.lower()}: {summary}", current_activity="")
        return {"run_id": self.run_id, "status": status, "error": error, "stats": dict(self.stats)}

    async def _safe_capture(self, name: str) -> str | None:
        try:
            if self.engine.alive:
                return (await self.engine.capture_failure(name)).get("screenshot")
        except Exception:
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
    async def process_queue(self, portals: list[str]) -> None:
        with session_scope() as s:
            queued = s.scalars(select(Job).where(Job.status == JobStatus.QUEUED, Job.portal.in_(portals))
                               .order_by(Job.updated_at)).all()
            items = [(j.id, j.portal, _posting_from_job(j)) for j in queued]
        if not items:
            return
        self._event(None, "queue", f"{len(items)} queued job(s) to process")
        logged_in: set[str] = set()
        for job_id, portal, posting in items:
            await self.control.checkpoint()
            reason = self._limits_reached()
            if reason:
                self._event(None, "limit", f"Stopping applications: {reason}")
                return
            adapter = self._adapter(portal)
            if adapter is None:
                continue
            if portal not in logged_in:
                try:
                    await adapter.ensure_logged_in(DBHumanGate(self.control))
                    logged_in.add(portal)
                except LoginRequired as exc:
                    self._event(None, "login_required", f"{portal}: {exc.reason}", level="warning")
                    continue
            await self.apply_job(job_id, adapter, posting)

    # ----------------------------------------------------------- discovery
    async def discover_portal(self, portal: str) -> None:
        adapter = self._adapter(portal)
        if adapter is None:
            self._event(None, "portal_unknown", f"No adapter for portal '{portal}'", level="warning")
            return
        assert self.profile is not None
        self.control.activity(f"{adapter.display_name}: checking login")
        await adapter.ensure_logged_in(DBHumanGate(self.control))
        search = self.profile.search
        years = self.profile.professional.total_experience_years if search.use_experience_filter else None
        for keywords in search.queries:
            for location in (search.locations or [""]):
                await self.control.checkpoint()
                q = SearchQuery(keywords=keywords, location=location, experience_years=years,
                                posted_within_days=search.posted_within_days, max_pages=search.max_pages_per_query,
                                max_jobs=search.max_jobs_per_query, easy_apply_only=search.linkedin_easy_apply_only)
                label = f"{adapter.display_name}: '{keywords}'" + (f" in {location}" if location else "")
                self.control.activity(f"Searching {label}")
                self._event(None, "search", f"Searching {label}")
                found = 0
                try:
                    async for posting in adapter.search(q):
                        found += 1
                        self.stats["discovered"] += 1
                        await self.control.checkpoint()
                        try:
                            await self.handle_posting(adapter, posting, keywords)
                        except (StopRequested, LoginRequired):
                            raise
                        except Exception as exc:  # isolate each job
                            log.exception("Job %s failed", posting.url)
                            self.stats["job_errors"] += 1
                            shot = await self._safe_capture(f"job-{posting.portal}-{posting.external_id}")
                            self._mark_error(posting, exc, shot)
                except NavigationFailed as exc:
                    self._event(None, "search_failed", f"{label}: {exc}", level="error")
                self._event(None, "search_done", f"{label}: {found} job(s) found")

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
        assert self.profile is not None
        with session_scope() as s:
            job, created = upsert_job(s, posting, run_id=self.run_id, query=query)
            job_id = job.id
            if created:
                self.stats["new_jobs"] += 1
            if not created and job.status in _HANDLED:
                self.stats["seen_before"] += 1
                return
            if posting.already_applied:
                set_status(s, job, JobStatus.ALREADY_APPLIED, f"{adapter.display_name} shows this job as applied")
                self.stats["already_applied"] += 1
                return
            dup = find_duplicate(s, job)
            if dup is not None:
                set_status(s, job, JobStatus.DUPLICATE, f"Same role already handled via {dup.portal} (job #{dup.id})")
                add_event(s, job.id, "duplicate", job.status_reason, run_id=self.run_id)
                self.stats["duplicates"] += 1
                return
        self.control.activity(f"Reading: {posting.title} @ {posting.company}")
        if not posting.details_loaded:
            posting = await adapter.fetch_details(posting)
        with session_scope() as s:
            job, _ = upsert_job(s, posting, run_id=self.run_id, query=query)
            if posting.already_applied:
                set_status(s, job, JobStatus.ALREADY_APPLIED, f"{adapter.display_name} shows this job as applied")
                self.stats["already_applied"] += 1
                return
        matcher = JobMatcher(self.profile, self.assistant)
        result = await matcher.evaluate(posting.to_job_input())
        decision = Decision(result.decision)
        if result.blockers:
            why = "; ".join(result.blockers)
        elif decision == Decision.SKIP:
            why = f"Low match ({result.score}): " + "; ".join(result.reasons[:2])
        else:
            why = "; ".join(result.flags) or (result.reasons[0] if result.reasons else "")
        with session_scope() as s:
            job = s.get(Job, job_id)
            assert job is not None
            save_match(s, job, result.to_dict())
            add_event(s, job.id, "evaluated", f"{decision.value} (score {result.score}): {why}"[:1000],
                      run_id=self.run_id, data={"score": result.score, "decision": decision.value})
            if decision == Decision.SKIP:
                set_status(s, job, JobStatus.SKIPPED, why or "Not relevant")
                self.stats["skipped"] += 1
                return
            if decision == Decision.MANUAL_REVIEW:
                set_status(s, job, JobStatus.REVIEW_PENDING, why or "Needs your review")
                self.stats["review_pending"] += 1
                return
            set_status(s, job, JobStatus.QUEUED, f"Relevant (score {result.score})")
            self.stats["relevant"] += 1
        reason = self._limits_reached()
        if reason:
            self.stats["deferred"] += 1
            self._event(job_id, "deferred", f"Kept in queue: {reason}")
            return
        await self.apply_job(job_id, adapter, posting)
        delay = self.profile.search.delay_between_applications_s
        if delay and max(delay) > 0:
            await self._sleep(random.uniform(min(delay), max(delay)))

    # --------------------------------------------------------- application
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
            category = (job.match_details or {}).get("category", "frontend")
            resume = get_resume(profile, job.resume_id) or select_resume(profile, category, job.skills or [])
            if resume is not None and not resume.absolute_path.is_file():
                resume = select_resume(profile, category, job.skills or [])
            app = start_application(s, job, self.mode, resume_id=resume.id if resume else None,
                                    resume_path=str(resume.absolute_path) if resume else None)
            app_id = app.id
            add_event(s, job.id, "apply_started", f"Applying ({self.mode.value}) with resume "
                                                  f"'{resume.id if resume else 'none'}'", application_id=app_id,
                      run_id=self.run_id)
        self.control.activity(f"Applying: {posting.title} @ {posting.company}")

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

        blockers = [i.message for i in blocking_issues(profile)]
        ctx = ApplyContext(
            engine=self.engine, profile=profile,
            answerer=Answerer(profile, self.assistant, memory_lookup=memory_lookup_fn()),
            mode=self.mode, job=posting, resume_path=resume.absolute_path if resume else None,
            gate=DBHumanGate(self.control, job_id, app_id), log_event=log_event, record_answer=on_answer,
            on_submit_attempt=on_submit, submit_blockers=blockers,
        )
        main_page = self.engine.page
        external_page = None
        try:
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
                        j.external_url = result.external_url
                self._event(job_id, "external", f"Redirected to {result.external_url}", application_id=app_id)
                external_page = result.external_page
                if external_page is None and result.external_url:
                    external_page = await self.engine.new_page()
                    await self.engine.goto(result.external_url, page=external_page)
                if external_page is None:
                    result = ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED, "External application link missing")
                else:
                    result = await apply_external(self.engine, external_page, ctx)
        except StopRequested:
            self._finish_interrupted(job_id, app_id)
            raise
        except LoginRequired as exc:
            result = ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED, exc.reason)
        except ManualActionRequired as exc:
            result = ApplyResult(ApplyOutcome.MANUAL_ACTION_REQUIRED, exc.reason)
        except Exception as exc:
            log.exception("Application failed for job %s", job_id)
            shot = await self._safe_capture(f"apply-{job_id}")
            result = ApplyResult(ApplyOutcome.FAILED, f"{exc.__class__.__name__}: {exc}", screenshot=shot)
        finally:
            if external_page is not None and external_page is not main_page:
                try:
                    if not external_page.is_closed():
                        await external_page.close()
                except Exception:
                    pass
            try:
                if not main_page.is_closed():
                    self.engine.use_page(main_page)
            except Exception:
                pass
        self._finalize(job_id, app_id, result)
        return result

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
        }
        status = mapping[result.outcome]
        if status == JobStatus.APPLIED and not result.confirmation_text:
            status = JobStatus.MANUAL_ACTION_REQUIRED  # never claim success without a confirmation
            result.message = "Submitted but no confirmation captured - please verify"
        with session_scope() as s:
            job = s.get(Job, job_id)
            app = s.get(Application, app_id)
            assert job is not None and app is not None
            app.finished_at = utcnow()
            app.ats_platform = result.ats or app.ats_platform
            app.final_url = result.external_url or app.final_url or job.url
            app.submit_attempted = app.submit_attempted or result.submit_attempted
            app.last_step = result.outcome.value.lower()
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
                            "unresolved": result.unresolved})
        self.stats[f"result_{status.value.lower()}"] += 1
        log.info("Job %s -> %s (%s)", job_id, status.value, result.message)


_SUMMARY_PARTS = [
    ("result_applied", "applied"), ("result_dry_run", "dry-run"), ("result_manual_action_required", "need you"),
    ("result_failed", "failed"), ("relevant", "relevant"), ("review_pending", "to decide"), ("skipped", "skipped"),
    ("already_applied", "already applied"), ("duplicates", "duplicates"), ("deferred", "deferred by limits"),
    ("new_jobs", "new jobs"), ("login_required", "portal(s) needing login"), ("job_errors", "job errors"),
    ("recovered", "recovered"),
]


def _summary(stats: Counter[str]) -> str:
    parts = [f"{stats[k]} {label}" for k, label in _SUMMARY_PARTS if stats.get(k)]
    if stats.get("discovered"):
        parts.append(f"{stats['discovered']} listings scanned")
    return ", ".join(parts) or "nothing to do"


def _posting_from_job(job: Job) -> JobPosting:
    return JobPosting(portal=job.portal, external_id=job.external_id, url=job.url, title=job.title,
                      company=job.company, location=job.location, experience_text=job.experience_text,
                      salary_text=job.salary_text, posted_text=job.posted_text, description=job.description,
                      skills=list(job.skills or []), apply_type=job.apply_type, external_url=job.external_url,
                      details_loaded=bool(job.description))
