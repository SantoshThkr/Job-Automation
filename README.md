# Personal Job Assistant

A local-first assistant that searches **Naukri, LinkedIn and Instahyre**, decides which jobs are
really relevant to you (rules + local Ollama AI), and applies on the portal or on the company's
own application site (Greenhouse, Lever, Workday, SmartRecruiters). It keeps a full record of
every job, answer and outcome in a local dashboard.

Everything runs on your Mac: FastAPI + SQLite + Playwright + Next.js + Ollama. It costs ₹0/month,
and nothing leaves your machine except the applications you choose to send.

```
                    PERSONAL JOB ASSISTANT
                              │
                    ┌─────────┴─────────┐
                    │                   │
           Dashboard (Next.js)    Job Engine (app/engine/orchestrator.py)
            ↕ FastAPI + SQLite          │
                              ┌─────────┼─────────┐
                              │         │         │
                           Naukri   LinkedIn  Instahyre        (app/portals/*)
                              └─────────┼─────────┘
                                  Job Matcher                  (app/matching/*)
                                ┌───────┴───────┐
                              Rules          Ollama            (app/ai/*)
                                └───────┬───────┘
                         APPLY / SKIP / MANUAL_REVIEW
                                        │
                             Application (submit gate:
                         Dry Run / Review Mode / Auto Apply)
                              ┌─────────┴─────────┐
                         Portal Apply         External ATS     (app/ats/*)
                                    ┌──────────┬──┴───────┬──────────────┐
                               Greenhouse    Lever     Workday   SmartRecruiters
                                    └──────────┴──┬───────┴──────────────┘
                                           Form Automation     (app/browser/forms.py)
                                     Resume + Answers (profile → memory → AI)
                                                  │
                                    Submission → Confirmation
                                                  │
                                           Application DB
```

---

## 1. Project structure

```
JS-Job/
├── README.md, Makefile, .env.example, docker-compose.yml
├── config/
│   ├── profile.example.json      # template (committed)
│   └── profile.json              # YOUR facts + preferences (git-ignored)
├── resumes/                      # your resume PDFs (git-ignored)
├── data/                         # created at runtime: jobs.db, logs/, screenshots/, browser-profile/
├── scripts/  setup.sh  dev.sh  demo.sh
├── backend/
│   ├── requirements.txt, requirements-dev.txt, pyproject.toml
│   ├── app/
│   │   ├── main.py               # FastAPI app (dashboard API)
│   │   ├── agent.py              # browser agent process (python -m app.agent)
│   │   ├── cli.py                # init / check / login / run / evaluate / answer
│   │   ├── config.py             # settings from .env (PJA_*)
│   │   ├── db.py, models.py      # SQLite (WAL) + ORM: jobs, applications, Q&A, events, runs, agent state
│   │   ├── profile.py, resumes.py
│   │   ├── matching/             # skills taxonomy, JD analyzer, rules scorer, matcher (rules + AI)
│   │   ├── ai/                   # Ollama provider, job assistant, hallucination guard
│   │   ├── answers/              # question bank (profile facts), option mapping, answerer
│   │   ├── browser/              # Playwright engine, robust selectors, generic form filler
│   │   ├── portals/              # NaukriAdapter, LinkedInAdapter, InstahyreAdapter, registry
│   │   ├── ats/                  # ATS step engine, Greenhouse/Lever/Workday/SmartRecruiters, detector
│   │   ├── engine/               # orchestrator, control channel + human gate, recorder, ApplyContext
│   │   └── api/                  # REST routes
│   └── tests/                    # 203 tests, mock portal/ATS sites, realistic JDs
└── frontend/                     # Next.js dashboard
    ├── app/  page.tsx (dashboard) · jobs/ · jobs/[id]/ · review/ · settings/
    ├── components/  lib/
    └── Dockerfile (optional)
```

## 2. What is implemented

| Area | Details |
|---|---|
| Foundation | FastAPI API, SQLite (WAL, safe for API + agent processes), lightweight migrations, rotating logs (`data/logs/api.log`, `agent.log`), `.env` config, validated `profile.json` with hot reload, resume management (tags, per-job selection, upload) |
| Matching | Title category + seniority, skills taxonomy (100+ skills with aliases), required vs nice-to-have sections, **frontend/backend ratio**, framework fit (React vs Angular/Vue), experience range, location/remote/hybrid, salary floor, excluded keywords/companies. Score 0-100 → `APPLY` / `SKIP` / `MANUAL_REVIEW`, with a human-readable explanation. Ollama refines ambiguous cases but can never override a hard blocker. Without Ollama, the rules work alone. |
| Browser engine | Persistent Chromium session (log in once), multi-strategy targets (CSS / ARIA role / visible text / label), waits, infinite scroll, page-change detection, popups, retries on timeouts and network errors, crash recovery (browser and tab), screenshots + HTML on failures, pause/resume/stop checkpoints before every action, CAPTCHA/OTP detection |
| Naukri | Search with URL pagination (current + legacy markup), detail extraction, one-click apply, **screening chatbot** (text / radio / checkbox / chips), "Apply on company site" → ATS, already-applied, expired jobs, session-expiry detection |
| LinkedIn | Search (scrolling result list), job view, **Easy Apply multi-step** (contact, resume picker that reuses an uploaded resume, questions, review), unchecks "follow company", discards drafts when not submitting, external "Apply" → ATS, applied state, checkpoint/CAPTCHA detection |
| Instahyre | Matched opportunities + skill search, details, apply (one-click or follow-up modal with questions), applied state |
| External ATS | Detection by URL, iframe and DOM; Greenhouse (incl. embedded iframes), Lever, Workday (multi-step, listbox widgets, sign-in wall), SmartRecruiters (shadow DOM); generic step engine. Unknown ATS → `MANUAL_ACTION_REQUIRED`. **Never APPLIED without a detected confirmation.** |
| AI assistant | Job understanding (is it frontend? FE %, relevance, key skills, concerns), factual answers strictly from the profile, short written answers ("Why this role?", cover letter), JSON repair + schema validation, confidence scores, hallucination guard (numbers, names, skills not in profile), circuit breaker when Ollama is down |
| Answers | Saved answers (memory) → your custom answers → 40+ deterministic profile rules → AI → manual review. Options are mapped to select/radio choices (ranges, yes/no, decline-to-answer). Optional unknown questions are left blank, never guessed. |
| Dashboard | Stats (discovered, relevant, applications, skipped, failed, manual action, today, weekly), Start / Pause / Resume / Stop, Dry Run / Review Mode / Auto Apply, portal selection, live activity, approval banner, job table with filters, application detail (job info, JD, match explanation, resume, Q&A, status, timeline with screenshots, failure reason), "Needs you" queue, settings, resume upload, saved answers |
| Hardening | Per-job isolation (one failure never stops the run), duplicates (same portal id **and** same company+title across portals), daily/run limits, interrupted-run recovery (re-queue, or flag "verify manually" if a submit may have happened), single-agent lock, heartbeat, graceful SIGINT/SIGTERM |

## 3. Setup

Requirements: macOS, **Python 3.11–3.13**, **Node.js 20+**, and optionally **Ollama**.

```bash
cd ~/Desktop/JS-Job
make setup                      # = ./scripts/setup.sh
# installs backend/.venv, Playwright Chromium, dashboard deps, creates .env, data/ and config/profile.json

# optional local AI (recommended)
brew install ollama             # if not installed
ollama serve                    # if not already running
ollama pull qwen3:4b            # or set PJA_OLLAMA_MODEL to another local model
```

Then fill in your profile:

1. Edit **`config/profile.json`**: name, email, phone, LinkedIn/GitHub, city, total experience,
   notice period, current/expected CTC (LPA), skills (and `skills.years` per skill), education,
   work history, summary, preferred locations, target/excluded titles, search queries.
   Leave anything you don't want auto-answered as `null` / empty: it goes to manual review.
2. Copy your resume(s) to `resumes/` and check the `resumes` entries in `profile.json` (paths + tags).
3. Set `"profile_confirmed": true` once it's accurate. Real submissions are blocked until then
   (Dry Run works regardless).
4. `make check` validates the profile, resumes, Ollama and the browser.

## 4. Required `.env` values

None are strictly required; the defaults work. `cp .env.example .env` (done by `make setup`) and adjust:

| Variable | Default | Purpose |
|---|---|---|
| `PJA_PROFILE_PATH` | `config/profile.json` | your candidate profile |
| `PJA_DATA_DIR` | `data` | DB, logs, screenshots, browser session |
| `PJA_BROWSER_HEADLESS` | `false` | keep `false` so you can log in / solve CAPTCHA / OTP |
| `PJA_BROWSER_CHANNEL` | *(empty)* | `chrome` to drive your installed Google Chrome instead of Chromium |
| `PJA_AI_ENABLED` | `true` | use local Ollama |
| `PJA_OLLAMA_URL` / `PJA_OLLAMA_MODEL` | `http://localhost:11434` / `qwen3:4b` | local model |
| `PJA_AI_MIN_CONFIDENCE` | `0.75` | answers below this confidence need your review |
| `PJA_DEFAULT_MODE` | `dry_run` | initial mode |
| `PJA_REVIEW_WAIT_TIMEOUT_S` / `PJA_LOGIN_WAIT_TIMEOUT_S` | `900` / `600` | how long the agent waits for you |

Personal data (phone, email, resume path, LinkedIn/GitHub, notice period, salary) goes in
`config/profile.json`, not in `.env`.

## 5. Start the application

```bash
make dev          # API on http://127.0.0.1:8000 + dashboard on http://localhost:3000
```
(or separately: `make api` and `make web`). Open **http://localhost:3000**.

## 6. Start the browser agent

**First, log in to each portal once** (the session is saved in `data/browser-profile/`):

```bash
make login-naukri
make login-linkedin
make login-instahyre
```

Then either:

* **Dashboard**: choose the mode and portals, press **Start**. The API launches the agent
  process (`python -m app.agent`); a Chromium window opens. Use **Pause / Resume / Stop** at any time.
* **Terminal**: `make agent MODE=dry_run PORTALS=naukri,linkedin`, or
  `cd backend && .venv/bin/python -m app.agent --mode review --portals instahyre --repeat-minutes 120`.

If a session expires mid-run, the dashboard shows **"Login needed"**: log in in the agent's
browser window and press **Continue**.

## 7. Dry Run

Dashboard → **Dry Run** → Start, or `make agent MODE=dry_run`.
Searches, scores, opens application forms and fills what it can, then **stops before every
irreversible click** (Naukri/Instahyre "Apply" are themselves submissions, so they are never
clicked; LinkedIn Easy Apply drafts are discarded). Results show as status `DRY_RUN` with the
answers it would have used. Start here.

## 8. Review Mode

Dashboard → **Review Mode** → Start, or `make agent MODE=review`.
The agent fills each application and then waits. A yellow banner shows
*"Ready to submit - your approval needed"* with **Approve & submit** / **Reject**. You can also
fix fields in the browser window first, or submit by hand: the agent detects the confirmation.
If a question can't be answered confidently, it asks you to answer it in the browser and press **Continue**.

## 9. Auto Apply

Dashboard → **Auto Apply** (asks for confirmation) → Start, or `make agent MODE=auto`.
It submits only when **all** of these hold:
* the job decision is `APPLY` (or you approved it);
* every required question was answered with confidence ≥ `PJA_AI_MIN_CONFIDENCE` from your
  profile, saved answers or verified AI text;
* `profile.json` has no blocking issues (`profile_confirmed: true`, real name/email/phone, a resume).

Otherwise the job becomes **Manual action required** and the run continues. Limits live in
`profile.json → search` (`max_applications_per_run`, `max_applications_per_day`,
`delay_between_applications_s`). Mode changes in the dashboard apply from the next application.

### The "Needs you" page
* **Should I apply?** – jobs the matcher was unsure about (Apply / Skip).
* **Questions to answer** – answer once; it's saved and reused on every future form. Then **Retry** the job.
* **Manual action required** – unknown ATS, CAPTCHA, account sign-in, unconfirmed submissions:
  Open / Retry / Mark as applied / Skip.

## 10. Test results

```bash
make test         # full suite (~4-5 min, uses headless Chromium + local mock sites)
make test-fast    # unit tests only (no browser)
```

Last run: **203 passed** (124 unit + 79 browser) (Python 3.13, Playwright 1.63, macOS arm64). Also verified:
`next build` (TypeScript) passes, and a full demo run (agent spawned from the API against all mock
portals with the live local Ollama model) rendered correctly in every dashboard page.

| Suite | What it proves |
|---|---|
| `test_foundation.py` | config, profile validation/hot reload, resumes, DB upsert/dedupe, restart persistence, all API endpoints |
| `test_matching.py` | 17 realistic Indian job descriptions → expected APPLY/SKIP/REVIEW; parsers; AI refinement, AI unavailable / malformed / low-confidence, blockers beat AI |
| `test_answers.py`, `test_ai.py` | 30+ real application questions, options mapping, memory, no invented facts, hallucination guard, JSON repair, **live Ollama test** |
| `test_browser_engine.py` | navigation, robust selectors, waits/timeouts, network failure, infinite scroll, overlays, popups, pause/resume/stop, browser crash + closed tab recovery, persistent session, CAPTCHA/OTP detection, form scanner (labels, radios, selects, shadow DOM, file upload) |
| `test_naukri.py`, `test_linkedin.py`, `test_instahyre.py` | each adapter against a local replica of the portal: login/expiry, search + pagination, details, apply in every mode, chatbot / Easy Apply / modal questions, external redirect, already applied |
| `test_ats.py` | detection, Greenhouse, Lever, Workday (multi-step, sign-in wall), SmartRecruiters, unknown ATS, CAPTCHA/OTP after submit, unexpected redirect, AI-written answers |
| `test_engine_e2e.py` | the whole engine across portals: dry run, auto apply, review approvals through the DB gate, failure isolation, browser crash mid-run, stop, pause, recovery of interrupted applications, limits, login expiry, incomplete profile, answer-memory + retry |
| `test_agent_process.py` | the real agent process from the CLI and from the dashboard Start/Approve/Stop buttons |

Try it yourself without touching real portals: **`make demo`** (stop `make dev` first) runs
everything against the mock portals with a fake candidate and an isolated database.

## 11. Known limitations

* **Live portals were not exercised by the automated tests.** Naukri/LinkedIn/Instahyre need your
  logged-in session, so the adapters were verified against faithful local replicas of their markup
  and flows. Portals change their HTML often; the adapters use several fallback strategies per
  element, but expect occasional selector updates. Start with **Dry Run** and look at the
  screenshots in `data/screenshots/` if something is not found.
* Instahyre's markup is the least publicly documented of the three; its adapter relies more on
  generic/text strategies.
* Workday usually needs an account per employer: the agent stops at the sign-in wall (you can sign
  in during Review Mode and press Continue).
* Workday/Greenhouse rich widgets (custom dropdowns, date pickers, location autocompletes) are
  handled for common cases; unusual widgets end up as "needs your input" rather than guessed.
* "Already applied" across portals is detected by company + title; differently worded titles for the
  same role are treated as different jobs.
* Small local models (e.g. `qwen3:4b`) take a few seconds per ambiguous job and may be
  conservative; the rules remain the primary decision-maker.
* Docker is optional and only runs the dashboard (`docker compose up --build dashboard`, with
  `make api` running natively): the agent needs a visible browser, and SQLite's WAL mode can't be
  shared across the Docker VM boundary. The Docker image build was not run here (Docker daemon was
  off); `docker compose config` validates.
* Not affiliated with any portal. Automated applying may conflict with a site's terms of use; keep
  limits modest and use Review Mode when in doubt.

## 12. What needs you (manual login / CAPTCHA / OTP)

| Situation | What happens |
|---|---|
| First use of each portal | `make login-<portal>` once. Sessions persist in `data/browser-profile/`. |
| Session expired | Dashboard shows **Login needed**; log in in the agent's window → Continue. In Auto Apply, the portal is skipped for this run and the rest continue. |
| CAPTCHA / "verify you're human" / LinkedIn checkpoint | Detected; Review/Dry Run wait for you to solve it in the window, Auto Apply marks the job *Manual action required* (never bypassed). |
| OTP / verification code | Same as CAPTCHA. |
| Employer ATS account (Workday, some SmartRecruiters) | Sign-in wall → manual (or sign in during Review Mode). |
| Unknown application site | *Manual action required* with the URL and a screenshot. |
| Questions not in your profile | Listed under **Needs you → Questions to answer**; your answer is reused automatically. |
| Submitted but no confirmation seen | *Manual action required: verify on the portal.* Never counted as applied, never auto-retried. |

---

### Useful commands

```bash
cd backend
.venv/bin/python -m app.cli evaluate --title "Senior React Developer" --jd-file jd.txt --ai   # score a JD
.venv/bin/python -m app.cli answer "What is your notice period?"                               # test an answer
.venv/bin/python -m tests.mock_server --port 8765                                             # mock portals
```

### Extending

* **New ATS**: subclass `ATSAdapter` in `app/ats/adapters.py` (URL patterns, DOM signals, open/next/
  submit targets, success markers, optional `prefill`) and add it to `ATS_ADAPTERS` in `app/ats/registry.py`.
* **New portal**: implement `PortalAdapter` (`is_logged_in`, `search`, `fetch_details`, `apply`) in
  `app/portals/<name>.py` and register it in `app/portals/registry.py`.
