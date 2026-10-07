"""Command line helpers.

    python -m app.cli init                 # create data dirs, database and config/profile.json
    python -m app.cli check                # validate profile, resumes, AI, browser
    python -m app.cli login naukri         # open the browser to log in once (session is saved)
    python -m app.cli run --mode dry_run   # run the agent in the foreground
    python -m app.cli evaluate --title "React Developer" --jd-file jd.txt
    python -m app.cli answer "What is your notice period?"
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
        print(f"[FAIL] Playwright browser: {exc}\n       Run: playwright install chromium")
    return 0 if ok else 1


def cmd_login(a) -> int:
    from app.browser.engine import BrowserEngine
    from app.portals.registry import PORTALS, create_adapter

    if a.portal not in PORTALS:
        print(f"Unknown portal {a.portal}; choose from {', '.join(PORTALS)}")
        return 2

    async def run() -> int:
        engine = BrowserEngine(headless=False)
        await engine.start()
        adapter = create_adapter(a.portal, engine)
        assert adapter is not None
        try:
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
            print("Timed out waiting for login.")
            return 1
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
    from app.answers.answerer import Answerer
    from app.answers.question_bank import QuestionContext
    from app.profile import get_profile

    ans = asyncio.run(Answerer(get_profile(), JobAssistant() if a.ai else None).answer(
        QuestionContext(question=a.question, field_type=a.type, options=a.options or [], required=True)))
    print(json.dumps({"answer": ans.value, "confidence": round(ans.confidence, 3), "source": ans.source,
                      "needs_review": ans.needs_review, "note": ans.note}, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.cli", description="Personal Job Assistant")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(fn=cmd_init)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    lp = sub.add_parser("login")
    lp.add_argument("portal", choices=["naukri", "linkedin", "instahyre"])
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
    ap.set_defaults(fn=cmd_answer)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
