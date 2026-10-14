"""Command line helpers.

    python -m app.cli init                 # create data dirs, database and config/profile.json
    python -m app.cli check                # validate profile, resumes, AI, browser
    python -m app.cli login naukri         # open the browser to log in once (session is saved)
    python -m app.cli run --mode dry_run   # run the agent in the foreground
    python -m app.cli evaluate --title "React Developer" --jd-file jd.txt
    python -m app.cli answer "What is your notice period?"
    python -m app.cli try-apply https://careers.example.com/jobs/123   # navigator on any job page (Dry Run)
    python -m app.cli reevaluate [--ai] [--dry] [--skipped]   # re-score pending (or skipped) jobs with the current matcher
    python -m app.cli site-login set|list|check|delete [site] [--user NAME]   # sign-ins for employer sites (Keychain)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from app.config import get_settings


def cmd_init(_a) -> int:
    from app.db import init_db
    from app.profile import get_profile_store

    s = get_settings()
    s.ensure_dirs()
    init_db()
    store = get_profile_store()
    existed = store.path.exists()
    store.ensure_exists()
    print(f"Data directory : {s.data_path}")
    print(f"Database       : {s.db_url}")
    print(f"Profile        : {store.path} ({'existing' if existed else 'created from example'})")
    print(f"Resumes folder : {s.resumes_path}")
    print("\nNext: edit config/profile.json, put your resume PDF(s) in resumes/, then run `python -m app.cli check`.")
    return 0


def cmd_check(_a) -> int:
    from playwright.sync_api import sync_playwright

    from app.ai.provider import get_provider
    from app.profile import get_profile_store, validate_profile
    from app.resumes import list_resumes

    ok = True
    store = get_profile_store()
    try:
        profile = store.load(force=True)
    except Exception as exc:
        print(f"[FAIL] profile.json: {exc}")
        return 1
    issues = validate_profile(profile)
    errors = [i for i in issues if i.severity == "error"]
    print(f"[{'OK' if not errors else 'FAIL'}] profile.json ({len(errors)} blocking, "
          f"{len(issues) - len(errors)} warnings)")
    for i in issues:
        print(f"    - {i.severity.upper():7} {i.field}: {i.message}")
    ok &= not errors
    for r in list_resumes(profile):
        print(f"[{'OK' if r.exists else 'MISSING'}] resume '{r.id}': {r.path}")
    h = asyncio.run(get_provider().health(use_cache=False))
    if h["available"] and h["model_present"]:
        print(f"[OK] Ollama model {h['model']}")
    else:
        print(f"[WARN] AI: {h['error'] or 'unavailable'} (the assistant still works with rules only)")
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            b.close()
        print("[OK] Playwright Chromium")
    except Exception as exc:
        ok = False
        print(f"[FAIL] Playwright browser: {exc}\n       Run: .venv/bin/python -m playwright install chromium")
    st = get_settings()
    print(f"[{'OK' if st.navigator_enabled else 'OFF'}] Navigator for company sites"
          f" (AI fallback {'on' if st.navigator_use_ai and st.ai_enabled else 'off'})")
    if st.ats_account_password:
        print("[OK] Employer-site account password set: sign-in"
              + (" and account creation" if st.ats_create_accounts else "") + " handled automatically")
    else:
        print("[INFO] PJA_ATS_ACCOUNT_PASSWORD not set: no general password for employer career sites")
    from app import site_logins as credentials

    if credentials.keychain_available():
        sites = credentials.stored_sites()
        print(f"[{'OK' if sites else 'INFO'}] Sign-ins stored in the macOS Keychain: "
              + (", ".join(site for site, _ in sites) if sites else "none (python -m app.cli site-login set <site>)"))
    else:
        print("[INFO] Per-site sign-ins (macOS Keychain) are not available or switched off (PJA_SITE_LOGIN_KEYCHAIN)")
    print(f"[{'OK' if st.google_signin else 'OFF'}] 'Sign in with Google' on career sites: the assistant opens "
          "Google's page and waits for you there" if st.google_signin else
          "[OFF] 'Sign in with Google' buttons are left alone (PJA_GOOGLE_SIGNIN=false)")
    print(f"[INFO] Auto Apply waits {st.auto_human_wait_s}s for you before parking a job; notifications "
          f"{'on' if st.notify else 'off'}")
    level, said = api_port_report(st.api_port)
    print(f"[{level}] {said}")
    return 0 if ok else 1


def port_listeners(port: int) -> list[tuple[str, int, str]]:
    """(program, pid, address) of everything listening on a TCP port of this machine ([] when it cannot be read)."""
    import shutil
    import subprocess

    if not shutil.which("lsof"):
        return []
    try:
        out = subprocess.run(["lsof", "-nP", f"-iTCP:{int(port)}", "-sTCP:LISTEN"], capture_output=True, text=True,  # noqa: S603, S607
                             timeout=10, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    seen: dict[tuple[int, str], tuple[str, int, str]] = {}
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 9 and parts[1].isdigit():
            address = parts[8]
            seen.setdefault((int(parts[1]), address), (parts[0], int(parts[1]), address))
    return list(seen.values())


def who_answers(port: int) -> str:
    """'ours' when the Job Assistant API answers on the loopback address of ``port``, 'other' when something else
    does, 'none' when nothing does."""
    import json
    import urllib.error
    import urllib.request

    from app.config import SERVICE_NAME

    try:
        # (a fixed http:// loopback address - no other scheme can be opened here)
        with urllib.request.urlopen(f"http://127.0.0.1:{int(port)}/api/health", timeout=3) as r:  # noqa: S310  # nosec B310
            raw = r.read(4096)
    except urllib.error.HTTPError:
        return "other"  # something answered - with an error: not this API
    except OSError:
        return "none"
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        return "other"  # something answered - not in this API's words
    return "ours" if isinstance(body, dict) and body.get("service") == SERVICE_NAME else "other"


def api_port_report(port: int) -> tuple[str, str]:
    """Is the API port ours alone? (level, sentence) for `check`. A second program listening on it (on 2026-10-13,
    another project's container on 8000, then this API's port) receives the dashboard's requests whenever this API
    is not running - the dashboard refuses to talk to it, but nothing works until the API is back."""
    who = who_answers(port)
    listeners = port_listeners(port)
    ours = [x for x in listeners if x[2].startswith(("127.0.0.1:", "[::1]:", "localhost:"))] if who == "ours" else []
    others = [x for x in listeners if x not in ours]
    names = ", ".join(sorted({f"{name} (pid {pid}, {address})" for name, pid, address in others}))
    own_port = ("Give this app a port of its own: set PJA_API_PORT to a free port in .env (the API and the dashboard both read "
                "it) and restart `make dev`.")
    if who == "ours" and not others:
        return "OK", f"API port {port}: the Job Assistant API is answering, and nothing else listens on it"
    if who == "ours":
        return "WARN", (f"API port {port} is shared: the Job Assistant API answers on it now, but {names} listens on it "
                        f"too and would answer in its place whenever this API is stopped. {own_port}")
    if who == "other":
        return "WARN", (f"API port {port} is answered by another program" + (f" ({names})" if names else "")
                        + f", not by the Job Assistant API: the dashboard cannot work like this. Start the API "
                          f"(make dev). {own_port}")
    if others:
        return "WARN", f"API port {port}: the API is not running, and {names} listens on that port. {own_port}"
    return "INFO", f"API port {port}: the API is not running (start it with: make dev)"


_STATE_MESSAGES = {
    "AUTHENTICATED": "signed in",
    "VERIFICATION": "LinkedIn security check (code / CAPTCHA / verification) - complete it in the browser window",
    "LOGIN_PAGE": "sign-in page - enter your email and password",
    "EXPIRED": "sign-in page (your previous session expired) - sign in again",
    "LOGGED_OUT": "guest page - click 'Sign in'",
    "UNKNOWN": "waiting for a LinkedIn page",
}


async def wait_for_linkedin_login(adapter, engine, timeout_s: int, *, poll_s: float = 2.0,
                                  out=print) -> tuple[bool, object, list[str]]:
    """Wait (without navigating the tab you are using) until LinkedIn shows an authenticated session.

    Returns (signed_in, last LoginCheck, states seen). CAPTCHA / OTP / verification stay manual: a security
    check is reported, never treated as success; the wait resumes by itself once you finish it."""
    from app.portals.linkedin_session import LinkedInState

    loop = asyncio.get_running_loop()
    start = loop.time()
    end = start + timeout_s
    history: list[str] = []
    check = None
    while loop.time() < end:
        if not engine.alive:
            out("The browser window was closed before the sign-in was detected.")
            return False, check, history
        check = await adapter.login_state(navigate=False)
        if not history or history[-1] != check.state.value:
            history.append(check.state.value)
            where = f" ({check.url})" if check.url else ""
            out(f"  [{int(loop.time() - start):>3}s] {_STATE_MESSAGES[check.state.value]}{where}")
        if check.state == LinkedInState.AUTHENTICATED:
            return True, check, history
        await asyncio.sleep(poll_s)
    return False, check, history


# cookies Google sets for a signed-in browser (names only are ever looked at - never their values)
GOOGLE_SESSION_COOKIES = frozenset({"SID", "__Secure-1PSID", "__Secure-3PSID", "SAPISID"})


def has_google_session(cookie_names) -> bool:
    return bool(GOOGLE_SESSION_COOKIES & set(cookie_names))


async def _login_google(timeout: int) -> int:
    """Sign in to Google once in the agent's own browser profile, so that "Sign in with Google" on career sites
    finds your session. Everything on Google's page is done by you; this only opens the page and waits."""
    from app.browser.engine import BrowserEngine

    engine = BrowserEngine(headless=False)
    await engine.start()

    async def signed_in() -> bool:
        return has_google_session(c["name"] for c in await engine.context.cookies("https://accounts.google.com"))

    try:
        if await signed_in():
            print(f"The agent's browser is already signed in to Google. The session is kept in {engine.user_data_dir}")
            return 0
        await engine.goto("https://accounts.google.com/")
        print("Sign in to Google in the browser window (password, 2-step verification - all yours; nothing is typed "
              "for you).")
        print(f"Waiting up to {timeout} seconds...")
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            if await signed_in():
                print(f"Signed in. The session is kept in {engine.user_data_dir} and reused when a career site "
                      "offers 'Sign in with Google'.")
                await asyncio.sleep(2)  # let the browser write the session to the profile
                return 0
            await asyncio.sleep(2)
        print("No Google sign-in was detected in time. If Google refused this browser (\"This browser or app may not be "
              "secure\"), sign-in with Google cannot be used here: store a sign-in for the site instead "
              "(python -m app.cli site-login set <site>) or sign in on that site by hand when asked.")
        return 1
    finally:
        await engine.stop()


def cmd_login(a) -> int:
    from app.browser.engine import BrowserEngine
    from app.portals.registry import PORTALS, create_adapter

    if a.portal == "google":
        return asyncio.run(_login_google(a.timeout))
    if a.portal not in PORTALS:
        print(f"Unknown portal {a.portal}; choose from {', '.join(PORTALS)} (or 'google')")
        return 2

    async def run_linkedin(adapter, engine) -> int:
        from app.portals.linkedin_session import LinkedInState, guidance

        first = await adapter.login_state(navigate=True)
        if first.authenticated:
            print(f"Already logged in to LinkedIn ({', '.join(first.signals)}). "
                  f"Session is saved in {engine.user_data_dir}")
            return 0
        if first.state != LinkedInState.VERIFICATION:  # never navigate away from a security check
            await engine.goto(adapter.login_url)
        print("Log in to LinkedIn in the browser window (complete any OTP/CAPTCHA/verification yourself).")
        print(f"Waiting up to {a.timeout} seconds - progress is shown below:")
        ok, check, history = await wait_for_linkedin_login(adapter, engine, a.timeout)
        if ok:
            print(f"Logged in to LinkedIn ({', '.join(check.signals)}).")
            print(f"The session is saved in {engine.user_data_dir} and reused by the agent.")
            await asyncio.sleep(2)  # let the browser write the session to the profile
            return 0
        if check is not None:
            print(f"LinkedIn sign-in was not detected within {a.timeout} s.")
            print(f"  Last state seen : {check.describe()}")
            print(f"  States seen     : {' -> '.join(history) or '-'}")
            hint = guidance(check, history)
            if hint:
                print(f"  What to do      : {hint}")
        return 1

    async def run_generic(adapter, engine) -> int:
        if await adapter.is_logged_in():
            print(f"Already logged in to {adapter.display_name}. Session is saved in {engine.user_data_dir}")
            return 0
        await engine.goto(adapter.login_url)
        print(f"Log in to {adapter.display_name} in the browser window (complete any OTP/CAPTCHA).")
        print(f"Waiting up to {a.timeout} seconds...")
        loop = asyncio.get_running_loop()
        end = loop.time() + a.timeout
        while loop.time() < end:
            if await adapter.is_logged_in(navigate=False):
                print("Logged in. The session is saved for the agent.")
                await asyncio.sleep(2)
                return 0
            await asyncio.sleep(2)
        url = engine.page.url if engine.alive and not engine.page.is_closed() else "browser closed"
        print(f"Timed out waiting for {adapter.display_name} login. The browser was on: {url.split('?')[0]}")
        return 1

    async def run() -> int:
        engine = BrowserEngine(headless=False)
        await engine.start()
        adapter = create_adapter(a.portal, engine)
        assert adapter is not None
        try:
            if hasattr(adapter, "login_state"):
                return await run_linkedin(adapter, engine)
            return await run_generic(adapter, engine)
        finally:
            await engine.stop()

    return asyncio.run(run())


def cmd_run(a) -> int:
    from app.agent import main as agent_main

    argv = []
    if a.mode:
        argv += ["--mode", a.mode]
    if a.portals:
        argv += ["--portals", a.portals]
    if a.repeat_minutes is not None:
        argv += ["--repeat-minutes", str(a.repeat_minutes)]
    return agent_main(argv)


def cmd_evaluate(a) -> int:
    from app.ai.job_assistant import JobAssistant
    from app.matching.analyzer import JobInput
    from app.matching.matcher import JobMatcher
    from app.profile import get_profile

    jd = Path(a.jd_file).read_text() if a.jd_file else (a.jd or "")
    matcher = JobMatcher(get_profile(), JobAssistant() if a.ai else None)
    result = asyncio.run(matcher.evaluate(JobInput(title=a.title, description=jd, location=a.location or "",
                                                   experience_text=a.experience or ""), use_ai=a.ai))
    d = result.to_dict()
    print(f"Decision: {d['decision']}   score: {d['score']}   frontend: {d['frontend_ratio']}")
    for r in d["reasons"]:
        print(f"  + {r}")
    for b in d["blockers"]:
        print(f"  ! {b}")
    for f in d["flags"]:
        print(f"  ? {f}")
    if a.json:
        print(json.dumps(d, indent=2))
    return 0


def cmd_answer(a) -> int:
    from app.ai.job_assistant import JobAssistant
    from app.answers.answerer import MODE_LABEL, Answerer, describe_sources
    from app.answers.question_bank import QuestionContext
    from app.engine.recorder import memory_lookup_fn
    from app.matching.analyzer import JobInput
    from app.profile import get_profile

    job = JobInput(title=a.job_title, company=a.company, description=a.jd or "") if a.job_title else None
    ans = asyncio.run(Answerer(get_profile(), JobAssistant() if a.ai else None, memory_lookup=memory_lookup_fn()).answer(
        QuestionContext(question=a.question, field_type=a.type, options=a.options or [], required=True, job=job)))
    print(json.dumps({"answer": ans.value, "category": ans.category, "mode": MODE_LABEL.get(ans.mode, ans.mode),
                      "answer_source": describe_sources(ans.sources), "sources": ans.sources,
                      "confidence": round(ans.confidence, 3), "asks_you": ans.needs_review, "note": ans.note},
                     indent=2, ensure_ascii=False))
    return 0


class ConsoleGate:
    """Human gate for the terminal: you answer questions / approve / confirm you finished a step."""

    def __init__(self) -> None:
        self.last_answers: dict[str, str] = {}

    async def wait(self, kind, reason, *, page=None, detect_done=None, timeout_s=None, payload=None):
        from app.engine.context import GateResult

        self.last_answers = {}
        print(f"\n>>> NEEDS YOU ({kind}): {reason}")
        if kind == "questions" and payload:
            for q in payload.get("questions", []):
                opts = f" {q['options']}" if q.get("options") else ""
                hint = f" [suggested: {q['suggested']}]" if q.get("suggested") else ""
                ans = (await asyncio.to_thread(input, f"    {q['question']}{opts}{hint}: ")).strip()
                if ans or q.get("suggested"):
                    self.last_answers[q["id"]] = ans or q["suggested"]
            return GateResult.RESOLVED
        prompt = "    Approve and submit? [y/N] " if kind == "review" else \
            "    Do it in the browser window, then press Enter (or type 's' to skip this job): "
        ans = (await asyncio.to_thread(input, prompt)).strip().lower()
        if kind == "review":
            return GateResult.APPROVE if ans in {"y", "yes"} else GateResult.REJECT
        return GateResult.REJECT if ans == "s" else GateResult.RESOLVED


def cmd_try_apply(a) -> int:
    """Run the application navigator on one job URL (company career page or any application form)."""
    from app.ai.job_assistant import JobAssistant
    from app.answers.answerer import Answerer
    from app.ats.external import apply_external
    from app.browser.engine import BrowserEngine
    from app.db import init_db, session_scope
    from app.engine.context import ApplyContext
    from app.engine.recorder import memory_lookup_fn, remember_answer
    from app.models import AgentMode
    from app.portals.types import JobPosting
    from app.profile import blocking_issues, get_profile
    from app.resumes import select_resume

    init_db()
    profile = get_profile()
    mode = AgentMode(a.mode)
    blockers = [i.message for i in blocking_issues(profile)]
    if blockers and mode != AgentMode.DRY_RUN:
        print("Profile incomplete - submissions are blocked:\n  - " + "\n  - ".join(blockers))
    resume = select_resume(profile, a.resume_category, [])
    job = JobPosting(portal="web", external_id=a.url, url=a.url, title=a.title, company=a.company)
    assistant = JobAssistant() if get_settings().ai_enabled else None

    async def log_event(type_, message, level="info", data=None):
        if type_ not in {"step"}:
            print(f"  [{type_}] {message}")

    async def on_step(description, url):
        print(f"  -> {description}")

    async def remember(question, answer):
        with session_scope() as s:
            remember_answer(s, question, answer)

    async def run() -> int:
        engine = BrowserEngine(headless=a.headless)
        await engine.start()
        try:
            ctx = ApplyContext(engine=engine, profile=profile,
                               answerer=Answerer(profile, assistant, memory_lookup=memory_lookup_fn()),
                               mode=mode, job=job, resume_path=resume.absolute_path if resume else None,
                               gate=ConsoleGate(), log_event=log_event, submit_blockers=blockers,
                               on_step=on_step, remember=remember, auto_human_wait_s=600)
            page = await engine.goto(a.url)
            result = await apply_external(engine, page, ctx)
            print(f"\nResult: {result.outcome.value} - {result.message}")
            if result.confirmation_text:
                print(f"Confirmation: {result.confirmation_text}")
            if result.unresolved:
                print("Unanswered: " + "; ".join(result.unresolved))
            for q, ans in ctx.answers_log:
                flag = " (needs review)" if ans.needs_review else ""
                print(f"  Q: {q.question[:70]!r} -> {ans.value!r} [{ans.source}]{flag}")
            if a.keep_open:
                await asyncio.to_thread(input, "\nPress Enter to close the browser...")
            return 0
        finally:
            await engine.stop()

    return asyncio.run(run())


_RETRYABLE_REASONS = ("Unknown application platform", "Profile incomplete", "Not submitted - needs your review",
                      "Could not find the Apply button")


def cmd_reevaluate(a) -> int:
    """Re-score jobs that are still pending with the current matcher; re-queue jobs parked only for reasons the
    assistant now handles (unknown company site, incomplete profile). Never touches anything that was submitted."""
    from sqlalchemy import select

    from app.ai.job_assistant import JobAssistant
    from app.db import init_db, session_scope
    from app.engine.recorder import add_event, save_match, set_status
    from app.matching.analyzer import JobInput
    from app.matching.matcher import JobMatcher
    from app.matching.rules import decision_reason, title_prescreen
    from app.models import Decision, Job, JobStatus
    from app.profile import get_profile

    init_db()
    profile = get_profile()
    rows: list[dict] | None = [] if a.list else None
    if a.skipped:
        # jobs skipped for a hard requirement under other preferences (the run does this by itself at its start)
        from app.engine.reconsider import reconsider_skipped

        moved = reconsider_skipped(profile, dry=a.dry, changes=rows)
        for k, v in sorted(moved.items()):
            print(f"{v:4}  skipped -> {k.replace('_', ' ')}")
        _print_changes(rows, a.list)
        print("(dry run: nothing changed)" if a.dry else "Done.")
        return 0
    from app.engine.recency import adjust as adjust_for_recency
    from app.engine.recency import current_window

    window = current_window()
    print(f"Job recency: {window.label()} (as chosen in the dashboard)")
    matcher = JobMatcher(profile, JobAssistant() if a.ai else None)
    pending = {JobStatus.NEW, JobStatus.QUEUED, JobStatus.REVIEW_PENDING, JobStatus.MANUAL_ACTION_REQUIRED,
               JobStatus.DRY_RUN}
    changes: dict[str, int] = {}
    with session_scope() as s:
        jobs = s.scalars(select(Job).where(Job.status.in_(list(pending)))).all()
        for job in jobs:
            app = job.application
            if job.status == JobStatus.MANUAL_ACTION_REQUIRED and (
                    (app is not None and app.submit_attempted)
                    or not any(r in (job.status_reason or "") for r in _RETRYABLE_REASONS)):
                continue  # needs you for a real reason, or something may have been submitted
            early = title_prescreen(profile, job.title)
            if early:
                result = None
                decision, why = Decision.SKIP, "; ".join(early)
            else:
                result = asyncio.run(matcher.evaluate(JobInput(
                    title=job.title, description=job.description, company=job.company, location=job.location,
                    experience_text=job.experience_text, salary_text=job.salary_text, skills=job.skills or [])))
                decision = Decision(result.decision)
                why = decision_reason(result, profile.matching.apply_threshold)
            decision, why, _age = adjust_for_recency(decision, why, job, window)
            new_status = {Decision.APPLY: JobStatus.QUEUED, Decision.SKIP: JobStatus.SKIPPED,
                          Decision.MANUAL_REVIEW: JobStatus.REVIEW_PENDING}[decision]
            if job.status == JobStatus.DRY_RUN and new_status == JobStatus.QUEUED:
                continue  # dry-run results stay as they are; approve them from the dashboard to apply
            key = f"{job.status} -> {new_status}"
            if job.status != new_status or result is not None:
                changes[key] = changes.get(key, 0) + 1
            if rows is not None and job.status != new_status:
                rows.append(change_row(job, str(new_status), why, result, profile))
            if a.dry:
                continue
            if result is not None:
                save_match(s, job, result.to_dict())
            if job.status != new_status:
                set_status(s, job, new_status, f"Re-evaluated: {why}"[:1000])
                add_event(s, job.id, "reevaluated", f"Re-evaluated with the current matcher: {decision.value} ({why})"[:1000])
    for k, v in sorted(changes.items()):
        print(f"{v:4}  {k}")
    _print_changes(rows, a.list)
    print("(dry run: nothing changed)" if a.dry else "Done.")
    return 0


def frontend_fit(result, profile) -> str:
    """Does the role match frontend-focused preferences? 'yes' | 'partly' | 'no' | '?' (from the evaluation)."""
    if result is None:
        return "no"  # ruled out by its title alone
    a = result.analysis or {}
    cat, ratio = a.get("title_category"), a.get("frontend_ratio")
    floor = profile.job_preferences.min_frontend_ratio
    if cat in {"backend", "mobile", "devops", "data", "qa"}:
        return "no" if ratio is None or ratio < floor else "partly"
    if ratio is None:
        return "?"
    if ratio >= floor:
        return "yes"
    return "partly" if ratio >= floor - 0.2 else "no"


def change_row(job, proposed: str, why: str, result, profile) -> dict:
    from app.matching.posting import describe_posted

    return {"job_id": job.id, "portal": job.portal, "company": (job.company or "").replace("\t", " "),
            "title": (job.title or "").replace("\t", " "), "current": str(job.status), "proposed": proposed,
            "reason": (why or "").replace("\n", " ").replace("\t", " "), "posted": describe_posted(job),
            "frontend_fit": frontend_fit(result, profile)}


def _print_changes(rows: list[dict] | None, where: str | None) -> None:
    """Every proposed status change, one tab-separated line each (to the file given with --list, or the screen)."""
    if rows is None:
        return
    head = ["job_id", "portal", "company", "title", "current", "proposed", "reason", "posted", "frontend_fit"]
    lines = ["\t".join(head)] + ["\t".join(str(r.get(k, "")) for k in head) for r in rows]
    if where and where != "-":
        from pathlib import Path

        Path(where).write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"{len(rows)} proposed change(s) written to {where}")
    else:
        print("\n".join(lines))


_EXP_Q = __import__("re").compile(r"years?|experience|\byoe\b|months?\s+of", __import__("re").I)
_STATE = {"APPLIED": "APPLIED", "MANUAL_ACTION_REQUIRED": "NEEDS_YOU", "AWAITING_APPROVAL": "NEEDS_YOU",
          "FAILED": "FAILED", "SKIPPED": "SKIPPED", "ALREADY_APPLIED": "SKIPPED", "EXPIRED": "SKIPPED",
          "DUPLICATE": "SKIPPED", "DRY_RUN": "SKIPPED", "QUEUED": "NEEDS_YOU", "PARKED": "NEEDS_YOU"}


def attempt_records(run_id: int) -> list[dict]:
    """Everything recorded about each application attempted in ``run_id`` (one dict per attempt)."""
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import ApplicationEvent, Job

    out = []
    with session_scope() as s:
        job_ids = list(dict.fromkeys(e.job_id for e in s.scalars(
            select(ApplicationEvent).where(ApplicationEvent.run_id == run_id, ApplicationEvent.type.in_(
                ["apply_started", "verify_started"])).order_by(ApplicationEvent.id))))
        for jid in job_ids:
            job = s.get(Job, jid)
            app = job.application
            evs = s.scalars(select(ApplicationEvent).where(ApplicationEvent.job_id == jid,
                                                          ApplicationEvent.run_id == run_id)
                            .order_by(ApplicationEvent.id)).all()
            started = next((e.id for e in evs if e.type in {"apply_started", "verify_started"}), 0)
            evs = [e for e in evs if e.id >= started]
            qas = [q for q in (app.answers if app else []) if q.created_at and evs and q.created_at >= evs[0].ts]
            exp = [{"question": q.question, "type": q.field_type, "options": (q.options or [])[:12], "entered": q.answer,
                    "verified": q.verified, "source": q.source, "note": q.note}
                   for q in qas if _EXP_Q.search(q.question or "") and (q.category or "") in {"EXPERIENCE", "UNKNOWN", ""}
                   or "entered as" in (q.note or "") or "experience levels" in (q.note or "")]
            submission = next((e for e in reversed(evs) if e.type == "submission"), None)
            after = next((e.data or {} for e in reversed(evs) if e.type == "resource"
                          and (e.data or {}).get("phase") == "after"), {})
            out.append({
                "portal": job.portal, "company": job.company, "title": job.title, "job_url": job.url,
                "application_url": (app.final_url if app else "") or "",
                "target": (app.apply_target if app else "") or "portal", "ats": (app.ats_platform if app else "") or "",
                "steps": sum(1 for e in evs if e.type == "step" and ("Next step" in e.message or "Filling" in e.message)) + 1,
                "questions": [f"{q.question} = {q.answer or '-'} [{q.source}]" for q in qas],
                "questions_needing_you": [q.question for q in qas if q.needs_review and q.required],
                "experience_fields": exp,
                "validation_failed": [f"{q.question}: {q.note}" for q in qas if "could not" in (q.note or "")
                                      or "did not accept" in (q.note or "")],
                "submit_reached": any(e.type == "submit" for e in evs),
                "submit_clicked": bool(app and app.submit_attempted),
                "after_submit": submission.message if submission else "",
                "confirmation": (app.verification_evidence if app else []) or [],
                "verification": (app.verification_state if app else "") or "",
                "final_state": _STATE.get(job.status, job.status), "status": job.status,
                "reason": job.status_reason if job.status != "APPLIED" else "",
                "confirmation_text": (app.confirmation_text if app else "") or "",
                "screenshot": next((e.screenshot for e in reversed(evs) if e.screenshot), "") or "",
                "errors": [e.message for e in evs if e.level == "error"][:3],
                "resources": {k: (round(v) if isinstance(v, float) else v) for k, v in after.items()
                              if k in {"application", "pages", "chromium_processes", "chromium_rss_mb",
                                       "renderer_max_mb", "python_rss_mb", "listeners", "tasks"}},
            })
    return out


def cmd_attempts(a) -> int:
    import json

    from app.db import init_db

    init_db()  # adds new columns to an older database

    rows = attempt_records(a.run)
    if a.json:
        print(json.dumps(rows, indent=1, default=str))
        return 0
    for r in rows:
        print(f"{r['final_state']:9} {r['portal']:9} {r['target']:8} {r['company'][:28]:28} {r['title'][:40]:40} "
              f"{(r['ats'] or '')[:24]:24} {r['reason'][:110]}")
    return 0


def cmd_site_login(a) -> int:
    """Sign-ins for employer career sites, kept in the macOS Keychain. Passwords are typed into macOS' own prompt:
    they are never an argument, never printed, never logged."""
    from app import site_logins as credentials
    from app.profile import get_profile

    if a.action == "list":
        sites = credentials.stored_sites()
        if not sites:
            print("No sign-in is stored. Add one with: python -m app.cli site-login set <site>")
        for site, user in sites:
            print(f"{site:40} {user}")
        return 0
    site = credentials.normalize_site(a.site or "")
    if not site:
        print("Give the site as a host name, e.g. careers.example.com (or paste the sign-in page's address)")
        return 2
    if a.action == "set":
        try:
            user = (a.user or "").strip() or get_profile().personal.email
        except Exception:  # noqa: BLE001 - no profile yet: the user name must be given
            user = (a.user or "").strip()
        if not user:
            print("Give the account's user name: --user you@example.com")
            return 2
        print(f"Storing the sign-in for {site} (user name: {user}) in your macOS Keychain.")
        print("macOS now asks for the password - type it there; it is not shown, logged or passed to this program.")
        try:
            status = credentials.store(site, user)
        except (ValueError, RuntimeError) as exc:
            print(f"Not stored: {exc}")
            return 2
        print("Stored." if status == 0 else "Not stored (the Keychain tool reported an error).")
        return 0 if status == 0 else 1
    if a.action == "check":
        from app.config import get_settings

        found = [key for key, _ in credentials.stored_sites() if key in credentials.site_keys(site)]
        if found:
            print(f"A sign-in is stored for {found[0]}: it is used on {site} (sign-in only, never to register).")
        elif get_settings().ats_account_password:
            print(f"No sign-in is stored for {site}: the general PJA_ATS_ACCOUNT_PASSWORD would be used with your "
                  "profile e-mail.")
        else:
            print(f"Nothing is configured for {site}: you are asked to sign in there yourself (or with Google, where "
                  "the site offers it).")
        return 0
    if a.action == "delete":
        print(f"Removed the sign-in for {site}." if credentials.delete(site) else f"No sign-in was stored for {site}.")
        return 0
    return 2


def cmd_resources(a) -> int:
    """[RESOURCE] samples of a run: run start, every 10 jobs read, before/after each application, run end."""
    from sqlalchemy import select

    from app.db import init_db, session_scope
    from app.models import ApplicationEvent

    init_db()
    with session_scope() as s:
        rows = [e.data or {} for e in s.scalars(select(ApplicationEvent).where(
            ApplicationEvent.run_id == a.run, ApplicationEvent.type == "resource").order_by(ApplicationEvent.id))]
    marks = {1, 5, 10, 15, 20, 25, 30}
    for d in rows:
        phase = d.get("phase", "")
        app = d.get("application")
        if a.all or phase in {"run_start", "run_end", "discovery"} or (phase == "after" and app in marks):
            tag = f"{phase}" + (f" app={app}" if app is not None else "") + (f" read={d.get('discovered')}"
                                                                           if d.get("discovered") else "")
            print(f"{tag:22} pages={d.get('pages')} chromium_processes={d.get('chromium_processes')} "
                  f"chromium_rss_mb={d.get('chromium_rss_mb', 0):.0f} renderer_max_mb={d.get('renderer_max_mb', 0):.0f} "
                  f"python_rss_mb={d.get('python_rss_mb', 0):.0f} listeners={d.get('listeners')} tasks={d.get('tasks')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.cli", description="Personal Job Assistant")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(fn=cmd_init)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    lp = sub.add_parser("login")
    lp.add_argument("portal", choices=["naukri", "linkedin", "instahyre", "google"])
    lp.add_argument("--timeout", type=int, default=600)
    lp.set_defaults(fn=cmd_login)
    rp = sub.add_parser("run")
    rp.add_argument("--mode", choices=["dry_run", "review", "auto"])
    rp.add_argument("--portals")
    rp.add_argument("--repeat-minutes", type=int)
    rp.set_defaults(fn=cmd_run)
    ep = sub.add_parser("evaluate")
    ep.add_argument("--title", required=True)
    ep.add_argument("--jd-file")
    ep.add_argument("--jd")
    ep.add_argument("--location")
    ep.add_argument("--experience")
    ep.add_argument("--ai", action="store_true")
    ep.add_argument("--json", action="store_true")
    ep.set_defaults(fn=cmd_evaluate)
    ap = sub.add_parser("answer")
    ap.add_argument("question")
    ap.add_argument("--type", default="text")
    ap.add_argument("--options", nargs="*")
    ap.add_argument("--ai", action="store_true")
    ap.add_argument("--job-title", default="", help="job context for written answers")
    ap.add_argument("--company", default="")
    ap.add_argument("--jd", default="", help="job description text")
    ap.set_defaults(fn=cmd_answer)
    tp = sub.add_parser("try-apply", help="run the application navigator on one job URL")
    tp.add_argument("url")
    tp.add_argument("--mode", choices=["dry_run", "review", "auto"], default="dry_run")
    tp.add_argument("--title", default="Frontend Engineer")
    tp.add_argument("--company", default="")
    tp.add_argument("--resume-category", default="frontend", choices=["frontend", "fullstack", "ai"])
    tp.add_argument("--keep-open", action="store_true", help="keep the browser open at the end")
    tp.add_argument("--headless", action="store_true", help="no visible browser window (you cannot help it then)")
    tp.set_defaults(fn=cmd_try_apply)
    rv = sub.add_parser("reevaluate", help="re-score pending jobs with the current matcher")
    rv.add_argument("--ai", action="store_true", help="also ask the local model (slower)")
    rv.add_argument("--dry", action="store_true", help="only show what would change")
    rv.add_argument("--skipped", action="store_true",
                    help="look again at recent jobs skipped for a hard requirement under older preferences")
    rv.add_argument("--list", nargs="?", const="-", default=None, metavar="FILE",
                    help="also list every proposed change (job, company, role, statuses, reason, posting date, "
                         "frontend fit) - on the screen, or tab-separated into FILE")
    rv.set_defaults(fn=cmd_reevaluate)
    sl = sub.add_parser("site-login", help="sign-ins for employer career sites (macOS Keychain)")
    sl.add_argument("action", choices=["set", "list", "check", "delete"])
    sl.add_argument("site", nargs="?", default="", help="the site, e.g. careers.example.com")
    sl.add_argument("--user", default="", help="the account's user name (default: your profile e-mail)")
    sl.set_defaults(fn=cmd_site_login)
    at = sub.add_parser("attempts", help="what happened in every application of a run")
    at.add_argument("--run", type=int, required=True)
    at.add_argument("--json", action="store_true")
    at.set_defaults(fn=cmd_attempts)
    rs = sub.add_parser("resources", help="browser / memory samples of a run")
    rs.add_argument("--run", type=int, required=True)
    rs.add_argument("--all", action="store_true")
    rs.set_defaults(fn=cmd_resources)
    a = p.parse_args(argv)
    import os

    from app.config import ConfigError

    os.umask(0o077)  # anything a command creates (profile, database, logs) is private to your account
    try:
        return a.fn(a)
    except ConfigError as exc:
        sys.stderr.write(f"\n{exc}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
