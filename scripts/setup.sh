#!/usr/bin/env bash
# One-time setup: Python venv + dependencies, Playwright Chromium, dashboard deps, config files, database.
set -euo pipefail
cd "$(dirname "$0")/.."

pick_python() {
  for p in ${PYTHON:-} python3.13 python3.12 python3.11 python3; do
    if command -v "$p" >/dev/null 2>&1; then
      v=$("$p" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
      case "$v" in 3.11|3.12|3.13) echo "$p"; return 0;; esac
    fi
  done
  return 1
}

PY=$(pick_python) || { echo "Python 3.11-3.13 is required (brew install python@3.13)"; exit 1; }
echo "==> Using $PY ($($PY --version))"
# (re)create the venv when missing or broken (e.g. the project folder was moved/renamed)
if ! backend/.venv/bin/python -c 'import sys' >/dev/null 2>&1; then
  rm -rf backend/.venv
  "$PY" -m venv backend/.venv
fi
backend/.venv/bin/python -m pip install -q --upgrade pip
backend/.venv/bin/python -m pip install -q -r backend/requirements-dev.txt
echo "==> Installing Playwright Chromium"
backend/.venv/bin/python -m playwright install chromium

echo "==> Installing dashboard dependencies"
command -v npm >/dev/null || { echo "Node.js 20+ is required (brew install node)"; exit 1; }
(cd frontend && npm install --no-fund --no-audit)

[ -f .env ] || { cp .env.example .env; echo "==> Created .env"; }
chmod 600 .env 2>/dev/null || true
# keep personal data and secrets out of Git: pre-commit / pre-push checks for this clone
if [ -d .git ]; then git config core.hooksPath .githooks && echo "==> Privacy hooks enabled (.githooks)"; fi
(cd backend && .venv/bin/python -m app.cli init)

if command -v ollama >/dev/null 2>&1; then
  MODEL=$(grep -E '^PJA_OLLAMA_MODEL=' .env | cut -d= -f2 | cut -d' ' -f1)
  MODEL=${MODEL:-qwen3:4b}
  if ! ollama list 2>/dev/null | awk '{print $1}' | grep -qx "$MODEL"; then
    echo "==> Ollama model $MODEL not found. Pull it with: ollama pull $MODEL"
  fi
else
  echo "==> Ollama not found (optional): brew install ollama && ollama pull qwen3:4b"
fi

cat <<'MSG'

Setup complete. Next:
  1. Edit config/profile.json (your details) and set "profile_confirmed": true
  2. Put your resume PDF(s) in resumes/ (paths are listed in profile.json)
  3. make check
  4. make login-naukri / make login-linkedin / make login-instahyre   (once; the session is saved)
  5. make dev   -> open http://localhost:3000 and press Start (Dry Run first)
MSG
