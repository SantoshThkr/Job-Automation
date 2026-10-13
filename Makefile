PY      := backend/.venv/bin/python
MODE    ?= dry_run
PORTALS ?=

.PHONY: help setup api web dev agent check login-naukri login-linkedin login-instahyre test test-fast build-web demo try reevaluate test-web privacy-check hooks audit

help:
	@echo "make setup            install everything (Python venv, Playwright Chromium, dashboard, config)"
	@echo "make dev              run API (:8000) + dashboard (:3000)"
	@echo "make agent MODE=...   run the browser agent in the foreground (dry_run | review | auto)"
	@echo "make login-naukri     log in once (also login-linkedin, login-instahyre)"
	@echo "make try URL=...      run the application navigator on one job page (MODE=dry_run|review|auto)"
	@echo "make reevaluate       re-score pending jobs with the current matcher (after changing preferences)"
	@echo "make check            validate profile, resumes, Ollama and browser"
	@echo "make test             run the full test suite (headless browser + mock portals)"
	@echo "make demo             try everything against local mock portals (isolated data)"
	@echo "make test-web         dashboard tests (Vitest) + typecheck"
	@echo "make privacy-check    verify no personal data / secret is visible to Git (files + full history)"
	@echo "make hooks            enable the pre-commit / pre-push privacy hooks for this clone"
	@echo "make audit            dependency + secret scans (pip-audit, npm audit, gitleaks when installed)"

setup:
	./scripts/setup.sh

api:
	cd backend && .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-server-header

web:
	cd frontend && npm run dev

dev:
	./scripts/dev.sh

agent:
	cd backend && .venv/bin/python -m app.agent --mode $(MODE) $(if $(PORTALS),--portals $(PORTALS),)

check:
	cd backend && .venv/bin/python -m app.cli check

login-naukri:
	cd backend && .venv/bin/python -m app.cli login naukri

login-linkedin:
	cd backend && .venv/bin/python -m app.cli login linkedin

login-instahyre:
	cd backend && .venv/bin/python -m app.cli login instahyre

test:
	cd backend && .venv/bin/python -m pytest -q

test-fast:
	cd backend && .venv/bin/python -m pytest -q -m "not browser"

build-web:
	cd frontend && npm run build

demo:
	./scripts/demo.sh

try:
	@test -n "$(URL)" || { echo 'usage: make try URL="https://careers.example.com/job/123" [MODE=dry_run|review|auto]'; exit 2; }
	cd backend && .venv/bin/python -m app.cli try-apply "$(URL)" --mode $(MODE) --keep-open

reevaluate:
	cd backend && .venv/bin/python -m app.cli reevaluate

test-web:
	cd frontend && npm run typecheck && npm test

privacy-check:
	@python3 scripts/privacy_check.py --all

hooks:
	git config core.hooksPath .githooks
	@echo "privacy hooks enabled (.githooks/pre-commit, .githooks/pre-push)"

audit: privacy-check
	cd frontend && npm audit --omit=dev
	@backend/.venv/bin/python -m pip_audit --version >/dev/null 2>&1 \
	  && backend/.venv/bin/python -m pip_audit -r backend/requirements.txt \
	  || echo "pip-audit not installed: backend/.venv/bin/python -m pip install pip-audit"
