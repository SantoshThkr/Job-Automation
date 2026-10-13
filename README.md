# Personal Job Assistant

A local, ₹0/month browser robot that searches **Naukri, LinkedIn and Instahyre** for frontend
(React / Next.js first) jobs, reads each job description to decide whether it fits you, and then
**operates a real browser to apply** — on the portal itself or on whatever company career site or
application system the job leads to. It adapts to what is on the screen instead of following one
fixed script, pauses for you only when something genuinely needs you (CAPTCHA, OTP, sign-in, an
unknown question), and records everything in a local dashboard.

Everything runs on your Mac: FastAPI + SQLite + Playwright + Next.js + Ollama (`qwen3:4b`).

```
             Dashboard (Next.js, :3000)  ⇄  FastAPI (:8000)  ⇄  SQLite (jobs, applications, answers, runs, live state)
                                                   │ start / pause / resume / stop, answers, approvals
                                          Agent process (python -m app.agent)
                                                   │
   ┌───────────────────────────── per portal ─────────────────────────────┐
   │ 1. Discover   Naukri / LinkedIn / Instahyre adapters (search + read)  │
   │ 2. Evaluate   rules (stack, FE share, skills, experience, location)   │
   │               + local AI reading the JD → APPLY / REVIEW / SKIP        │
   │ 3. Apply      best match first (React/Next.js > Angular-only, …)       │
   └──────────────────────────────────────────────────────────────────────┘
                                                   │
           Portal flow (Naukri chatbot, LinkedIn Easy Apply, Instahyre modal)
                                                   │ "Apply on company site"
                  Known ATS adapter (Greenhouse, Lever, Workday, SmartRecruiters)
                                                   │ unknown site / adapter stuck
              ┌────────────── Navigator: generic browser agent ──────────────┐
              │ observe → understand → decide → act → verify, every step:     │
              │ cookie banners, popups, new tabs, iframes, multi-step forms,  │
              │ guest vs sign-in, account creation, CAPTCHA/OTP (→ you),      │
              │ unknown questions (→ you, once), submit gate, confirmation    │
              │ rules first; local LLM picks among on-page elements if stuck  │
              └──────────────────────────────────────────────────────────────┘
                                                   │
                     Result: APPLIED (only with confirmation) / DRY_RUN / NEEDS YOU /
                             EXPIRED / ALREADY_APPLIED / FAILED  → dashboard + history
```

---

## 1. Quick start

```bash
cd ~/Desktop/Job-automation
make setup                 # Python venv, Playwright Chromium, dashboard deps, .env, database
ollama serve               # if not already running (separate terminal)
ollama pull qwen3:4b       # once

# fill in config/profile.json (see section 6), put your resume PDFs in resumes/
make check                 # validates profile, resumes, Ollama, browser

make login-naukri          # once per portal; the session is saved in data/browser-profile/
make login-linkedin
make login-instahyre

make dev                   # API http://127.0.0.1:8000 + dashboard http://localhost:3000
```

Open **http://localhost:3000**, choose **Dry Run**, press **Start**. When the results look right,
switch to **Review Mode**, then **Auto Apply**.

Try the navigator on any single job page (company career page, ATS link…) without running a search:

```bash
make try URL="https://careers.example.com/jobs/123"            # Dry Run: fills nothing irreversible
make try URL="https://careers.example.com/jobs/123" MODE=review  # asks you in the terminal before submitting
```

## 2. What happens during a run

1. **Search** every query × location on each selected portal (`profile.json → search`).
2. **Pre-screen titles** (excluded keywords, intern/director, QA/DevOps/data/mobile) without opening the page.
3. **Read the job** (full description, skills, experience, salary). Closed/expired postings are marked `EXPIRED`.
4. **Evaluate**: deterministic rules + local AI (always for generic or full-stack titles, otherwise only when
   borderline). Java/.NET/PHP full-stack roles are skipped unless the description is genuinely frontend-heavy;
   React + Python/Node is accepted; React/Next.js titles get a priority boost, Angular-only a small penalty;
   frontend + AI (LLM/RAG/agents) roles are recognised. Unclear jobs go to *Needs you → Should I apply?*.
5. **Apply, best match first**, within the run/day limits. The resume is chosen per job
   (frontend / frontend+AI / full-stack). The portal flow is used where the portal is the application
   (Naukri chatbot, LinkedIn Easy Apply, Instahyre); anything else continues on the company's site.
6. **Record** the outcome, answers, screenshots and a step-by-step timeline. A job is `APPLIED` only when
   confirmation text appeared *after* the submit click.

Duplicates (same posting, or same company + title already applied elsewhere) are never applied twice.

## 3. The navigator (company sites and unknown application systems)

`backend/app/navigator/` is a general browser agent. Each step it:

* **observes** the page: visible fields (main document, shadow DOM, embedded iframes), every clickable
  element (including JS-only "Apply" spans), dialogs, password boxes, CAPTCHA/OTP, page text;
* **understands** the situation: confirmation · already applied · closed/unavailable job · cookie banner ·
  promotional popup · sign-in / sign-up wall · application form · job page with an Apply button ·
  email-only application · unknown;
* **acts** with rules: fills fields from your profile + saved answers (radio, checkbox, select, comboboxes,
  file upload — also custom "Upload resume" buttons), ignores search boxes, newsletter boxes and honeypot
  anti-bot fields, prefers *Apply as guest*, picks the Apply button of *this* job on pages listing many;
* **falls back to the local LLM** only when the rules cannot tell what to do. The model sees a summary and a
  numbered list of on-page elements and may only pick one of them (invented ids are rejected);
* **verifies** the page changed; never clicks the same dead button twice; follows new tabs;
* **pauses for you** when needed and continues from the same page afterwards.

Every click that could send the application (Submit, Apply inside a form, Send, Finish, Save in an
application dialog, unknown submit buttons) goes through the mode gate (Dry Run / Review / Auto).

Known systems (Greenhouse, Lever, Workday, SmartRecruiters) use their specialised adapter first; if it gets
structurally stuck, the navigator continues from the same page.

**Employer accounts** (Workday, SuccessFactors, Taleo, iCIMS, Oracle…): set `PJA_ATS_ACCOUNT_PASSWORD` in `.env`
and the agent signs in with your profile email, or creates an account when a site requires one (never in
Dry Run). Email verification codes/links always come to you. Without the password it asks you to sign in.

## 4. Modes

| Mode | What it does |
|---|---|
| **Dry Run** | Searches, evaluates, opens applications and fills what it can, **never submits, never creates accounts, never waits for you** (it notes what would need you). |
| **Review** | Prepares each application, then waits for **Approve & submit** / **Reject** in the dashboard (you can also edit fields in the browser first). Multi-step "submit" flows need one approval per application. |
| **Auto Apply** | Submits when every required answer is confident. When it needs you it notifies you and waits `PJA_AUTO_HUMAN_WAIT_S` (default 180 s); if nobody responds the job is parked as *Needs you* and the run continues. |

Real submissions need a complete profile (name, email, phone, resume, `profile_confirmed: true`).
With an incomplete profile, relevant jobs simply stay **queued** (you are told once) — Dry Run still works.

## 5. How application questions are answered

One answer engine (`backend/app/answers/`) answers every question on Naukri's chatbot, LinkedIn Easy Apply,
Instahyre, known ATS forms and company sites. For each question it:

1. **Understands the intent** — "Why are you interested in this job?", "What attracts you to this role?" and
   "Why are you applying?" are all `WHY_ROLE`; "Describe your React background" is `TECHNICAL_EXPERIENCE`;
   "Thank you for your responses." is not a question at all. Local and deterministic; the local model only
   classifies wordings the rules do not recognise.
2. **Known facts** from your profile (city, company, CTC, last working day, links, education…).
3. **Derived facts**: joining date from notice period / serving notice / last working day, years with a skill
   from the dates of the roles that list it (React 6.5 years, Next.js 4.5…), relocation and city choices from
   your locations, whether you worked for an employer from your work history.
   The label is read the way a person reads it: `Notice - Period`, `mobilephone`, `current_ctc` and `D.O.B` are
   the ordinary labels they look like, and every wording of one question gets the same fact ("How many years
   have you worked with React?", "React.js experience", "How long have you used React in production?"). A
   skill's years are never given as your total experience, or the other way round. Lists of the company's
   openings ("Select position", "Apply for", "Job role") get the opening you are applying to - or are left for
   you when none of them clearly is it. "Select all that apply" skill lists get the options your profile lists.
4. **Your saved answers** — what you answered once is reused. Each answer has a reuse scope: **everywhere**, only
   on one **portal**, only for one **company**, or only for one **job**; one question can hold an answer per
   scope and the most specific one that applies wins (job, company, portal, everywhere). An answer saved for
   everywhere is also kept under a wording-independent key when the question has one, so "D.O.B" answers
   "Date of birth (DD/MM/YYYY)" and "Reason for change" answers "Why are you looking for a job change?".
   Saved answers are only ever written from what you typed - never from a generated or guessed answer - and can
   be corrected or deleted under *Settings → Saved answers*. A fact in your profile always wins over a saved
   answer.
5. **Written answers** (why this role / company, why hire you, strengths, experience with X, current
   responsibilities, a project, career goals, cover letter) are generated from your verified summary,
   highlights, work history and skills, emphasising what the job description asks for, sized to the field
   (one sentence for an input, 2–4 for a textarea, longer for a cover letter). The local model writes them
   when it is running; every draft is checked (no invented numbers, names, skills, employers; no technology
   credited to the wrong employer; no "led/managed" unless your profile says so) and replaced by a
   deterministic profile-based answer if it fails.
6. **Asks you** only when nothing above can answer truthfully: visas / work permits, ID numbers, date of birth,
   age, hometown, unknown employers, achievements your profile does not state, interview slots, legal
   declarations, amounts in another currency, in-hand monthly pay, contradictions in your profile. The local
   model is never asked to guess these.

Each answer is recorded with its category, mode (profile fact / derived / AI generated / written from profile /
your saved answer), sources and confidence, and the form control is read back to verify it took the value.
The activity log shows, per question:

```
Question detected: Why are you interested in this role?
Answer source: profile + job description
Answer mode: AI generated (grounded)
Confidence: 0.92
Filled answer · Verified
```

Try it: `cd backend && .venv/bin/python -m app.cli answer "Why should we hire you?" --type textarea --job-title "Senior React Developer" --company Acme --jd "React, Next.js, TypeScript" [--ai]`

## 6. When it needs you

| Situation | What happens |
|---|---|
| A required question that cannot be answered truthfully from your profile (see section 5) | The dashboard shows the question (with options); answer it there → it is filled in, the application continues, and the answer is **saved and reused** on every future form. |
| CAPTCHA / "verify you are human" / OTP | Never bypassed. Notification + banner; solve it in the agent's browser and press **Continue**. Inline "I'm not a robot" boxes are left for you right before submitting. |
| Company site requires sign-in / account | Signs in / creates the account if `PJA_ATS_ACCOUNT_PASSWORD` is set, otherwise asks you. Email verification always asks you. |
| Applications by email only | Marked *Needs you* with the address to send your resume to. |
| The Apply button does nothing / page not understood | It tries once, asks the local AI, then asks you to move it one step forward; it continues from there. |
| Parked company-site application | **Retry** continues on the page where it stopped instead of restarting. Answering its pending questions in *Needs you* re-queues it automatically. |
| Submitted but no confirmation seen | *Needs you: verify on the site.* Never counted as applied, never auto-retried. A confirmation needs the site's own evidence: its confirmation message, a reference, its answer to the submit request (including WordPress Contact Form 7's `mail_sent`) - and never one weak signal alone. Advertising pixels and bot-detection requests are not submissions. |
| The only form on the page is a contact / sales enquiry form | Never filled or sent as an application ("Inquire now", "Get a quote", or a form that takes no résumé and asks nothing about a job). *Needs you*, with that reason. |
| A portal stops taking applications (daily limit, or it stops registering Apply clicks) | Nothing was sent, so nothing is parked on you: the job stays **queued**, that portal's own applications pause for the rest of the run (company-site applications continue), and the run log says what the portal answered. |
| The site says a value is too long | The same text is shortened at a sentence end inside the limit and the form is sent once. |
| Portal session expired | *Login needed* banner; log in in the agent's window and press Continue. |
| `make login-linkedin` | Sign in (and pass LinkedIn's security check) in the window; progress is printed (sign-in page → security check → signed in) and it finishes by itself once LinkedIn creates the session. Detection uses the session cookie, URL, page structure and every open tab, never a single selector; a security check is never treated as success. If it gives up it prints the last state seen and what to do (e.g. LinkedIn looping back to sign-in after the check → try `PJA_BROWSER_CHANNEL=chrome`). |

On macOS you get a notification whenever the agent waits for you (`PJA_NOTIFY`).

## 7. Your profile and resumes (`config/profile.json`)

The profile is the only source of facts about you; nothing is invented. Fill in:

* `personal`: first/last name, email, phone, city, LinkedIn, GitHub, portfolio (optional ones may stay empty).
* `professional`: current title/company, experience (7), notice period, current/expected CTC (LPA),
  summary, highlights, work history, education.
* `skills`: already filled from your stated skills — adjust years per skill in `skills.years`.
* `resumes`: put your PDFs at the listed paths — `resumes/resume-frontend.pdf` (default),
  `resumes/resume-ai.pdf` (frontend + AI roles), `resumes/resume-fullstack.pdf` (frontend-heavy full stack).
  Missing files are skipped automatically.
* `custom_answers`: fixed answers for recurring questions (`"pattern"` substring or `"re:"` regex).
* `projects` (optional): `[{"name", "company", "description", "technologies", "outcome"}]` — lets "tell us about a
  project / your biggest achievement" be answered from real projects (outcomes only if true).
* `work_preferences.interview_availability` (optional): e.g. "Available for interviews on weekdays" — otherwise
  interview-slot questions are asked.
* `work_preferences.open_to_shifts` / `willing_to_travel` (optional, `true` / `false`): answer night-shift,
  US-hours and travel questions; while unset they are asked.
* `professional.work_history_complete` (optional): set it to `true` when `work_history` names every employer you
  have worked for. "Have you worked for us before?" is then answered **No** for any other company; while unset
  that question is always left for you (an employer in your history is always answered Yes).
* `skills.years`: years for skills your work history does not date (a skill listed without years is asked once).
* `skills.complete` (optional): set it to `true` when `primary` + `secondary` list every skill you would claim.
  "Years of experience with <a technology you do not list>" is then answered **0** and "Do you have experience
  with it?" **No**; while unset those answers are only suggested and the question is left for you.
* `work_preferences.citizenship` (optional): countries you are a citizen of, e.g. `["India"]`. Status questions
  ("Work Authorization: Citizen / Permanent Resident / Work Visa", "Citizenship") are answered only from this —
  `authorized_countries` answers Yes/No "authorized to work" questions but never implies citizenship.
* Set `"profile_confirmed": true` when it is accurate.

Optional fields left empty are simply asked about (once) when a form requires them.

## 8. `.env` settings (all optional)

| Variable | Default | Purpose |
|---|---|---|
| `PJA_OLLAMA_MODEL` / `PJA_OLLAMA_URL` | `qwen3:4b` / `http://127.0.0.1:11434` | local model |
| `PJA_OLLAMA_KEEP_ALIVE` | `30s` | how long the model stays in memory after a request (it takes about 3 GB while loaded; Ollama's own default of 5 minutes keeps it loaded for a whole run). It is also unloaded when a run ends. |
| `PJA_MEMORY_GUARD` / `PJA_MEMORY_WAIT_S` | `true` / `120` | before each search and each application the run reads the Mac's memory state (kernel pressure level, swap in use, free disk - macOS can only add swap while the startup disk has room). Under pressure it continues **without loading the AI model**; when memory is exhausted it waits, then stops and leaves the remaining jobs queued. Nothing of yours is ever closed. |
| `PJA_AI_MODEL_MEMORY_MB` | `3200` | what loading the local model takes. With less memory headroom than that (plus a 2 GB margin) the model is **not loaded**: the run goes on with the rules and your saved answers, and says so once. A model that is already in memory stays in use. `0` switches the check off. |
| `PJA_BROWSER_DISK_CACHE_MB` | `100` | cap of the agent browser's HTTP cache on disk (your sessions are not affected) |
| `PJA_AI_MIN_CONFIDENCE` | `0.75` | answers below this are asked, not guessed |
| `PJA_AUTO_HUMAN_WAIT_S` | `180` | Auto Apply: wait for you before parking a job (0 = never wait) |
| `PJA_NOTIFY` | `true` | macOS notifications |
| `PJA_NAVIGATOR_ENABLED` / `PJA_NAVIGATOR_USE_AI` | `true` / `true` | generic company-site agent / AI fallback |
| `PJA_NAVIGATOR_MAX_STEPS` / `PJA_NAVIGATOR_MAX_MINUTES` | `45` / `15` | per-application budget |
| `PJA_ATS_ACCOUNT_PASSWORD` | empty | password for employer career-site accounts (use one only for job sites) |
| `PJA_ATS_CREATE_ACCOUNTS` | `true` | allow creating those accounts (never in Dry Run) |
| `PJA_BROWSER_HEADLESS` | `false` | keep visible so you can help with CAPTCHA/OTP/sign-in |
| `PJA_BROWSER_CHANNEL` | empty | `chrome` to drive your installed Google Chrome |
| `PJA_REVIEW_WAIT_TIMEOUT_S` / `PJA_LOGIN_WAIT_TIMEOUT_S` | `900` / `600` | review / login waits |
| `PJA_API_HOST` / `PJA_API_PORT` | `127.0.0.1` / `8000` | where the API listens - keep it on loopback |
| `PJA_ALLOWED_HOSTS` | `localhost,127.0.0.1,::1,host.docker.internal` | host names the API answers to |
| `PJA_API_TOKEN` | empty | shared secret dashboard ⇄ API; **required** when `PJA_API_HOST` is not loopback |
| `PJA_API_DOCS` | `false` | `/docs` and `/openapi.json` (development only) |
| `PJA_RATE_LIMIT_ENABLED` | `true` | request limits (strictest for agent start, AI calls, uploads) |
| `PJA_MAX_REQUEST_BYTES` / `PJA_MAX_UPLOAD_BYTES` | 1 MB / 10 MB | request body / resume upload limits |

An invalid or unsafe setting stops the API and the agent at start-up with a message naming the variable.

Search, matching and limits live in `profile.json → search / matching / job_preferences`.

## 9. Dashboard

* **Dashboard**: jobs found, relevant, applications, skipped, failed, *needs your attention*, queued,
  today/this week; Start / Pause / Resume / Stop; mode and portals; the **Now** panel (current job, phase,
  current application step, page); live activity; recent applications; run history.
* **Needs you**: live questions to answer, approvals, *Should I apply?* decisions, parked jobs (Open / Retry /
  Mark applied / Skip).
* **Jobs**: every job with status, score, decision, resume, filters (incl. *Closed / expired*).
* **Job detail**: description, match explanation, resume used, every question and answer (with source),
  step-by-step timeline with screenshots, failure reason, where a parked application will continue.
* **Settings**: profile check, resumes, saved answers.

## 10. Commands

```bash
make dev                                   # API + dashboard
make agent MODE=dry_run PORTALS=naukri     # agent in the foreground (dry_run | review | auto)
make try URL="https://…" [MODE=review]     # navigator on one job page, prompts in the terminal
make reevaluate                            # re-score pending jobs after changing preferences (--ai via the CLI)
make test                                  # full test suite (headless browser + local mock sites)
make test-fast                             # unit tests only
make test-web                              # dashboard tests (Vitest) + typecheck
make privacy-check                         # is any personal data / secret visible to Git? (files + full history)
make audit                                 # privacy check + npm audit + pip-audit
make hooks                                 # enable the pre-commit / pre-push privacy hooks (done by make setup)
make demo                                  # whole system against local mock portals, fake candidate
cd backend && .venv/bin/python -m app.cli evaluate --title "Senior React Developer" --jd-file jd.txt --ai
cd backend && .venv/bin/python -m app.cli answer "What is your notice period?"
```

## 11. Tests

`make test` runs 998 backend tests (last run: 998 passed; ~42 min in one session - never run two sessions at once, and never beside a real run) and `make test-web` 28 dashboard tests: the unit suite, the security suite and the browser suite (headless Chromium against local mock portals,
ATS pages and 16 mock company career sites reproducing real-world situations: cookie banners, multi-step
SPA forms, guest-vs-sign-in, login walls, account creation with email verification, popups, iframe
forms, inline CAPTCHA, closed/unavailable jobs, already-applied pages, email-only applications,
placeholder-labelled forms with honeypots, multi-job listings, dead Apply buttons, JS-only buttons,
post-submit questionnaires, new tabs, and AI-fallback pages).

### Memory

A run costs about 0.5-1.2 GB for the agent's browser (one browser, one context, one tab per application), under
0.1 GB for the Python process and about 3 GB while the local AI model is loaded. Every `[RESOURCE]` line in the run
log (and `python -m app.cli resources <run>`) records the browser's and Python's footprint (resident + compressed, as
Activity Monitor shows it), the model's memory, swap in use, the kernel's pressure level and free disk space, before
and after each application, and every ten listings while a portal is searched (searching LinkedIn is the heaviest
phase: 1.2-1.6 GB for the browser). If macOS says "Your system has run out of application memory", check free disk
space first: with a full startup disk macOS cannot create swap files, and every open application then counts.

The model is the one large, optional cost, so it is the one the run gives up first: it is not loaded when there is no
room for it, it is unloaded 30 seconds after its last use and at the end of a run, and it is paused while macOS
reports memory pressure. Without it the run uses the rules and your saved answers only (as it does when Ollama is
not running): written answers and pages the rules do not recognise are left for you.

## 12. Known limitations

* **Live portals were not run by the automated tests** (they need your logged-in session). Portal adapters were
  verified against local replicas; the navigator was additionally probed (without filling or submitting)
  on the real company sites from your earlier runs. Start with Dry Run and check the timeline/screenshots.
* Sites that require an account (Accenture, Workday tenants, SuccessFactors…) need either
  `PJA_ATS_ACCOUNT_PASSWORD` or you signing in; email verification is always yours.
* Very custom widgets (date pickers, multi-select tag inputs, drag-and-drop-only uploads) may need you.
* Small local models are slow (~5–10 s per call) and conservative; rules decide most cases, the model is a fallback.
* Email-only applications are reported, not sent.
* Automated applying may conflict with a site's terms of use; keep limits modest and prefer Review Mode at first.

## 13. Extending

* **New ATS adapter** (optional — the navigator already handles unknown sites): subclass `ATSAdapter` in
  `app/ats/adapters.py` and add it to `ATS_ADAPTERS` in `app/ats/registry.py`.
* **New portal**: implement `PortalAdapter` (`is_logged_in`, `search`, `fetch_details`, `apply`) in
  `app/portals/<name>.py` and register it in `app/portals/registry.py`.
* **Navigator vocabulary**: button/page phrases live in `app/navigator/classify.py`.

## 14. Security & privacy

This is a single-user tool that runs on your machine. There are no accounts: the protection is about making
sure only *you, on this machine* can reach it, and that your data never leaves it by accident.

**Your data stays out of Git**

* `config/profile.json`, `.env`, `data/` (database, backups, browser sessions, logs, screenshots) and `resumes/`
  are git-ignored. `config/profile.example.json` and `.env.example` are the templates and contain placeholders only.
* `make privacy-check` scans everything Git can see - tracked files, untracked-but-not-ignored files and the
  **full history** - for private files, for *your own* details (read locally from your profile; findings name the
  kind of value, never the value) and for credential patterns (plus [gitleaks](https://github.com/gitleaks/gitleaks)
  when installed).
* The same check runs as `pre-commit` and `pre-push` hooks (`.githooks/`, enabled by `make setup` / `make hooks`).
  Bypass on purpose only (`--no-verify`). Remember: `.gitignore` does not untrack a file that is already tracked
  and does not rewrite history.
* Tests and fixtures use synthetic people only. Never paste real details into source, tests or fixtures.

**Who can reach the dashboard and the API**

* Both listen on `127.0.0.1` only (`make dev`, `npm run dev|start`, `docker compose`). They are not reachable
  from your network.
* Host allow-list (API and dashboard): a request for any other host name is refused - this stops DNS rebinding.
* Cross-site requests (CSRF) are refused: `Sec-Fetch-Site: cross-site` or a foreign `Origin`.
* `PJA_API_TOKEN` (optional on loopback): a shared secret the dashboard *server* adds to each API call; it is
  never sent to the browser. If you set `PJA_API_HOST` to a non-loopback address the API refuses to start
  without it. Put the same value in `.env` (API) and in the dashboard's environment (`make dev` does this).
* Request bodies are bounded; rate limits apply per client (strictest for starting the agent, AI evaluations and
  uploads). The limiter is in-memory, i.e. exact for the single API process this project runs.
* Responses carry `nosniff`, no-framing, `no-referrer`, `no-store` (API) and a Content-Security-Policy. The
  dashboard's CSP allows only its own origin; `'unsafe-inline'` is required by Next.js' bootstrap scripts.
* Uploaded resumes must really be PDF/DOC/DOCX; scraped links are only followed / shown when they are http(s).
* The agent's browser profile (`data/browser-profile`) holds your logged-in portal sessions: treat `data/` like
  a password store. Data files are created with owner-only permissions (0600 / 0700).
* Secrets (`PJA_ATS_ACCOUNT_PASSWORD`, `PJA_API_TOKEN`) live only in `.env`, are redacted from logs and are never
  returned by the API.

**Not covered** - anything running under your own macOS account can read these files (use FileVault for disk
encryption at rest); the database itself is not encrypted; there is no multi-user access control.
