#!/usr/bin/env bash
# Run the API (http://127.0.0.1:8010) and the dashboard (http://localhost:3000) together. Ctrl+C stops both.
# (8010 rather than the common 8000, which other programs - e.g. another project's container - often occupy.)
# Both listen on this machine only (loopback): the dashboard reads your profile and controls the agent.
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077  # files created while running (database, logs, screenshots) are private to your account

# The API reads .env itself; the dashboard server needs two of its values too (never sent to the browser).
env_value() { { [ -f .env ] && grep -E "^$1=" .env | tail -1 | cut -d= -f2- | sed -E 's/[[:space:]]+#.*$//; s/^["'"'"']//; s/["'"'"']$//'; } || true; }
PORT="${PJA_API_PORT:-$(env_value PJA_API_PORT)}"; PORT="${PORT:-8010}"
export PJA_BACKEND_URL="${PJA_BACKEND_URL:-http://127.0.0.1:$PORT}"
TOKEN="${PJA_API_TOKEN:-$(env_value PJA_API_TOKEN)}"
if [ -n "$TOKEN" ]; then export PJA_API_TOKEN="$TOKEN"; fi

(cd backend && exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" --no-server-header) &
API=$!
(cd frontend && exec npm run dev) &
WEB=$!
trap 'kill $API $WEB 2>/dev/null; wait 2>/dev/null' INT TERM EXIT
echo "API: http://127.0.0.1:$PORT   Dashboard: http://localhost:3000   (this machine only)"
wait
