#!/usr/bin/env bash
# Try the whole system safely against LOCAL MOCK portals (Naukri/LinkedIn/Instahyre/ATS copies),
# with a fake candidate profile and an isolated database. Nothing is sent to real websites.
# Stop `make dev` first (the demo uses ports 8010, 3000 and 8765).
set -euo pipefail
cd "$(dirname "$0")/.."
DEMO="${TMPDIR:-/tmp}/pja-demo"
rm -rf "$DEMO" && mkdir -p "$DEMO/resumes" "$DEMO/data"
printf '%%PDF-1.4\n%% demo resume\n' > "$DEMO/resumes/resume-frontend.pdf"
cp "$DEMO/resumes/resume-frontend.pdf" "$DEMO/resumes/resume-fullstack.pdf"
backend/.venv/bin/python - "$DEMO" <<'PY'
import json, sys
demo = sys.argv[1]
d = json.load(open("backend/tests/fixtures/profile_test.json"))
d["resumes"][0]["path"] = f"{demo}/resumes/resume-frontend.pdf"
d["resumes"][1]["path"] = f"{demo}/resumes/resume-fullstack.pdf"
d["matching"]["use_ai"] = True
d["search"]["delay_between_applications_s"] = [1, 3]
json.dump(d, open(f"{demo}/profile.json", "w"), indent=2)
PY
export PJA_DATA_DIR="$DEMO/data" PJA_PROFILE_PATH="$DEMO/profile.json" PJA_RESUMES_DIR="$DEMO/resumes"
export PJA_BROWSER_PROFILE_DIR="$DEMO/data/browser-profile"
export PJA_NAUKRI_BASE_URL=http://127.0.0.1:8765/naukri PJA_LINKEDIN_BASE_URL=http://127.0.0.1:8765/linkedin
export PJA_INSTAHYRE_BASE_URL=http://127.0.0.1:8765/instahyre
export PJA_BROWSER_HEADLESS="${HEADLESS:-false}"

(cd backend && exec .venv/bin/python -m tests.mock_server --port 8765) &
MOCK=$!
export PJA_BACKEND_URL=http://127.0.0.1:8010  # the dashboard talks to the demo API, nothing else
(cd backend && exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8010 --no-server-header) &
API=$!
(cd frontend && exec npm run dev) &
WEB=$!
trap 'kill $MOCK $API $WEB 2>/dev/null; wait 2>/dev/null' INT TERM EXIT
echo
echo "DEMO running against mock portals. Open http://localhost:3000 and press Start (try Review Mode)."
echo "Demo data lives in $DEMO (deleted on the next demo run). Ctrl+C to stop."
wait
