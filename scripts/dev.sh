#!/usr/bin/env bash
# Run the API (http://127.0.0.1:8000) and the dashboard (http://localhost:3000) together. Ctrl+C stops both.
set -euo pipefail
cd "$(dirname "$0")/.."
(cd backend && exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "${PJA_API_PORT:-8000}") &
API=$!
(cd frontend && exec npm run dev) &
WEB=$!
trap 'kill $API $WEB 2>/dev/null; wait 2>/dev/null' INT TERM EXIT
echo "API: http://127.0.0.1:${PJA_API_PORT:-8000}   Dashboard: http://localhost:3000"
wait
