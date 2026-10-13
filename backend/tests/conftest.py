"""Shared test fixtures: isolated data dir / DB / profile, fake Ollama, local mock-site server."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

FIXTURES = Path(__file__).parent / "fixtures"
SITES = FIXTURES / "sites"

def _setup_env(base: Path) -> None:
    os.environ["PJA_DATA_DIR"] = str(base / "data")
    os.environ["PJA_PROFILE_PATH"] = str(base / "profile.json")
    os.environ["PJA_RESUMES_DIR"] = str(base / "resumes")
    os.environ["PJA_BROWSER_PROFILE_DIR"] = str(base / "data" / "browser-profile")
    os.environ["PJA_DATABASE_URL"] = f"sqlite:///{base / 'data' / 'test.db'}"
    os.environ["PJA_AI_ENABLED"] = "false"
    os.environ["PJA_AGENT_SPAWN_ENABLED"] = "false"
    os.environ["PJA_BROWSER_HEADLESS"] = "true"
    os.environ["PJA_HUMAN_DELAY_MS_MIN"] = "0"
    os.environ["PJA_HUMAN_DELAY_MS_MAX"] = "0"
    os.environ["PJA_NAV_TIMEOUT_MS"] = "8000"
    os.environ["PJA_ACTION_TIMEOUT_MS"] = "3000"
    os.environ["PJA_REVIEW_WAIT_TIMEOUT_S"] = "20"
    os.environ["PJA_LOGIN_WAIT_TIMEOUT_S"] = "5"
    os.environ["PJA_AUTO_HUMAN_WAIT_S"] = "0"
    os.environ["PJA_NOTIFY"] = "false"
    # the memory guard reads the real machine: tests that run the agent must not depend on how busy this Mac is
    # (tests/test_memory_guard.py switches it on and feeds it known states)
    os.environ["PJA_MEMORY_GUARD"] = "false"
    os.environ["PJA_ATS_ACCOUNT_PASSWORD"] = ""
    # API protection: Starlette's TestClient addresses the app as "testserver"; the rate limiter is switched on by
    # the security tests themselves so unrelated tests never depend on how many requests ran before them
    os.environ["PJA_ALLOWED_HOSTS"] = "testserver,localhost,127.0.0.1"
    os.environ["PJA_RATE_LIMIT_ENABLED"] = "false"
    os.environ["PJA_API_TOKEN"] = ""
    os.environ["PJA_API_HOST"] = "127.0.0.1"


def write_test_profile(base: Path) -> Path:
    resumes = base / "resumes"
    resumes.mkdir(parents=True, exist_ok=True)
    (resumes / "resume-frontend.pdf").write_bytes(b"%PDF-1.4\n% test frontend resume\n")
    (resumes / "resume-fullstack.pdf").write_bytes(b"%PDF-1.4\n% test fullstack resume\n")
    data = json.loads((FIXTURES / "profile_test.json").read_text())
    data["resumes"][0]["path"] = str(resumes / "resume-frontend.pdf")
    data["resumes"][1]["path"] = str(resumes / "resume-fullstack.pdf")
    path = base / "profile.json"
    path.write_text(json.dumps(data, indent=2))
    return path


@pytest.fixture(scope="session", autouse=True)
def test_env(tmp_path_factory) -> Iterator[Path]:
    base = tmp_path_factory.mktemp("pja")
    _setup_env(base)
    write_test_profile(base)
    from app.ai.provider import set_provider
    from app.config import reset_settings_cache
    from app.db import init_db, reset_engine
    from app.profile import set_profile_store

    reset_settings_cache()
    reset_engine()
    set_profile_store(None)
    set_provider(None)
    init_db()
    yield base
    reset_engine()


@pytest.fixture
def profile():
    from app.profile import get_profile_store

    return get_profile_store().load(force=True)


@pytest.fixture
def clean_db():
    from app.db import session_scope
    from app.models import AgentState, AnswerMemory, Application, ApplicationEvent, Job, QuestionAnswer, Run

    with session_scope() as s:
        for model in (QuestionAnswer, ApplicationEvent, Application, Job, AnswerMemory, Run):
            s.query(model).delete()
        st = AgentState.get(s)
        st.desired_state = "STOPPED"
        st.actual_state = "IDLE"
        st.mode = "dry_run"
        st.gate_kind = None
        st.gate_response = None
        st.pending_job_id = None
        st.portals = []
    yield


# ------------------------------------------------------------- fake AI ---

def fake_ollama(responses: list, *, tags_ok: bool = True, model: str = "test-model"):
    """OllamaProvider whose HTTP calls are answered from ``responses`` (str content, dict, or Exception)."""
    from app.ai.provider import OllamaProvider

    queue = list(responses)
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            if not tags_ok:
                raise httpx.ConnectError("refused")
            return httpx.Response(200, json={"models": [{"name": model}]})
        body = json.loads(request.content or b"{}")
        calls.append(body)
        if not queue:
            return httpx.Response(200, json={"message": {"content": "{}"}})
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        content = item if isinstance(item, str) else json.dumps(item)
        return httpx.Response(200, json={"message": {"role": "assistant", "content": content}})

    provider = OllamaProvider(base_url="http://ollama.test", model=model, enabled=True,
                              transport=httpx.MockTransport(handler))
    provider.calls = calls  # type: ignore[attr-defined]
    return provider


# ------------------------------------------------------- mock web sites ---

@pytest.fixture(scope="session")
def site_server() -> Iterator[str]:
    """Serve tests/fixtures/sites over HTTP on a free local port."""
    from tests.mock_server import serve

    server, url = serve(0)
    yield url
    server.shutdown()


def ollama_is_running() -> bool:
    try:
        r = httpx.get("http://localhost:11434/api/tags", timeout=2)
        return r.status_code == 200 and bool(r.json().get("models"))
    except Exception:
        return False
