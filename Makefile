PY      := backend/.venv/bin/python
MODE    ?= dry_run
PORTALS ?=

.PHONY: help setup api web dev agent check login-naukri login-linkedin login-instahyre test test-fast build-web demo

help:
	@echo "make setup            install everything (Python venv, Playwright Chromium, dashboard, config)"
	@echo "make dev              run API (:8000) + dashboard (:3000)"
	@echo "make agent MODE=...   run the browser agent in the foreground (dry_run | review | auto)"
	@echo "make login-naukri     log in once (also login-linkedin, login-instahyre)"
	@echo "make check            validate profile, resumes, Ollama and browser"
	@echo "make test             run the full test suite (headless browser + mock portals)"
	@echo "make demo             try everything against local mock portals (isolated data)"

setup:
	./scripts/setup.sh

api:
	cd backend && .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000

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
