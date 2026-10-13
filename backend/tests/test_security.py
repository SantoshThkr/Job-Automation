"""Security regression tests for the dashboard API (app/security.py, app/config.py, the route models).

The application is single-user and local: there are no accounts to tell apart, so "authorization" here means that a
request really comes from the dashboard on this machine - right host, not cross-site, the token when one is set.
Every protection is tested with hostile input *and* with the normal traffic it must not disturb.
"""

from __future__ import annotations

import logging
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import ConfigError, get_settings, reset_settings_cache
from app.db import session_scope
from app.models import AnswerMemory, Job, JobStatus
from app.security import RedactingFilter, harden_permissions, host_name, is_web_url, limiter, rate_rule

# synthetic values, assembled so that no credential-shaped literal exists in the repository (privacy_check.py)
TOKEN = "-".join(("test", "token", "k9" * 16))  # 43 characters
ATS_SECRET = "-".join(("Synthetic", "Ats", "Pass", "7x" * 6))


@contextmanager
def api(**env: str) -> Iterator[TestClient]:
    """The app built with extra settings (PJA_* environment variables), restored afterwards."""
    from app.main import create_app

    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    reset_settings_cache()
    limiter.reset()
    try:
        with TestClient(create_app()) as client:
            yield client
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        reset_settings_cache()
        limiter.reset()


def _job(title: str = "Frontend Engineer", company: str = "KitCo") -> int:
    with session_scope() as s:
        job = Job(portal="naukri", external_id=f"sec-{title}-{company}", url="https://example.test/job", title=title,
                  company=company, location="Noida", fingerprint=f"fp-{title}-{company}", status=JobStatus.NEW)
        s.add(job)
        s.flush()
        return job.id


# ----------------------------------------------------------------------------------- configuration (fail safe)

@pytest.mark.parametrize("env,problem", [
    ({"PJA_API_HOST": "0.0.0.0"}, "PJA_API_HOST is not a loopback address"),  # exposed without authentication
    ({"PJA_API_HOST": "192.168.1.20"}, "PJA_API_TOKEN"),
    ({"PJA_API_TOKEN": "too-short"}, "at least 24 characters"),
    ({"PJA_API_PORT": "not-a-number"}, "PJA_API_PORT"),
    ({"PJA_API_PORT": "70000"}, "PJA_API_PORT"),
    ({"PJA_CORS_ORIGINS": "*"}, "wildcard origin is not allowed"),
    ({"PJA_CORS_ORIGINS": "http://localhost:3000,javascript:alert(1)"}, "PJA_CORS_ORIGINS"),
    ({"PJA_ALLOWED_HOSTS": "*"}, "PJA_ALLOWED_HOSTS"),
    ({"PJA_DEFAULT_MODE": "yolo"}, "PJA_DEFAULT_MODE"),
    ({"PJA_LOG_LEVEL": "LOUD"}, "PJA_LOG_LEVEL"),
    ({"PJA_OLLAMA_URL": "file:///etc/passwd"}, "PJA_OLLAMA_URL"),
    ({"PJA_MAX_UPLOAD_BYTES": "-5"}, "PJA_MAX_UPLOAD_BYTES"),
])
def test_unsafe_or_malformed_configuration_is_refused_with_a_clear_message(monkeypatch, env, problem):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    reset_settings_cache()
    try:
        with pytest.raises(ConfigError) as err:
            get_settings()
    finally:
        monkeypatch.undo()
        reset_settings_cache()
    assert problem in str(err.value) and "Invalid configuration" in str(err.value)


def test_a_configuration_error_never_prints_a_secret(monkeypatch):
    monkeypatch.setenv("PJA_API_TOKEN", TOKEN)
    monkeypatch.setenv("PJA_ATS_ACCOUNT_PASSWORD", ATS_SECRET)
    monkeypatch.setenv("PJA_API_PORT", "nope")
    reset_settings_cache()
    try:
        with pytest.raises(ConfigError) as err:
            get_settings()
    finally:
        monkeypatch.undo()
        reset_settings_cache()
    assert TOKEN not in str(err.value) and ATS_SECRET not in str(err.value)


def test_an_exposed_api_needs_a_token_and_then_starts(monkeypatch):
    monkeypatch.setenv("PJA_API_HOST", "0.0.0.0")
    monkeypatch.setenv("PJA_API_TOKEN", TOKEN)
    reset_settings_cache()
    try:
        assert get_settings().api_token == TOKEN
    finally:
        monkeypatch.undo()
        reset_settings_cache()


# ----------------------------------------------------------------------------------- who may call the API

@pytest.mark.parametrize("host,status", [
    ("testserver", 200), ("localhost:8000", 200), ("127.0.0.1:8000", 200), ("LOCALHOST", 200),
    ("evil.example.com", 400), ("attacker.test:8000", 400), ("127.0.0.1.evil.example", 400),
    ("localhost@evil.example", 400), ("192.168.1.20:8000", 400), ("", 400),
])
def test_only_expected_host_names_are_served(clean_db, host, status):
    with api() as c:
        r = c.get("/api/stats", headers={"host": host})
        assert r.status_code == status, r.text
        if status == 400:
            assert r.json() == {"detail": "Invalid Host header"}


@pytest.mark.parametrize("headers,status", [
    ({}, 200),  # the dashboard server and command-line tools send neither header
    ({"sec-fetch-site": "same-origin"}, 200),
    ({"sec-fetch-site": "same-site", "origin": "http://localhost:3000"}, 200),
    ({"sec-fetch-site": "none"}, 200),  # typed into the address bar
    ({"origin": "http://127.0.0.1:3000"}, 200),
    ({"origin": "http://testserver"}, 200),  # the API's own origin
    ({"sec-fetch-site": "cross-site"}, 403),
    ({"origin": "https://evil.example.com"}, 403),
    ({"origin": "null"}, 403),  # sandboxed frame / local file
    ({"origin": "http://localhost:3000.evil.example"}, 403),
])
def test_cross_site_requests_are_refused_for_reads_and_writes(clean_db, headers, status):
    with api() as c:
        assert c.get("/api/profile", headers=headers).status_code == status
        r = c.post("/api/agent/stop", headers=headers)  # a body-less POST: the classic CSRF shape
        assert r.status_code == status, r.text
        if status == 403:
            assert r.json() == {"detail": "Cross-site requests are not allowed"}


def test_cors_names_the_dashboard_only(clean_db):
    with api() as c:
        ok = c.options("/api/stats", headers={"origin": "http://localhost:3000", "access-control-request-method": "GET"})
        assert ok.headers.get("access-control-allow-origin") == "http://localhost:3000"
        assert "access-control-allow-credentials" not in ok.headers
        bad = c.options("/api/stats", headers={"origin": "https://evil.example.com",
                                               "access-control-request-method": "POST"})
        assert "access-control-allow-origin" not in bad.headers
        assert "PATCH" not in ok.headers.get("access-control-allow-methods", "")


def test_token_is_required_everywhere_once_configured(clean_db):
    job_id = _job()
    with api(PJA_API_TOKEN=TOKEN) as c:
        assert c.get("/api/health").status_code == 200  # liveness only, no data
        for method, path in [("get", "/api/profile"), ("get", "/api/stats"), ("get", f"/api/jobs/{job_id}"),
                             ("get", "/api/needs-you"), ("get", "/api/answers/memory"), ("get", "/api/agent"),
                             ("get", "/api/events"), ("get", "/api/resumes"), ("get", "/api/screenshots/x.png"),
                             ("post", "/api/agent/stop"), ("post", f"/api/jobs/{job_id}/action"),
                             ("delete", "/api/answers/memory/1"), ("put", "/api/agent/mode")]:
            r = getattr(c, method)(path)
            assert r.status_code == 401, (method, path, r.status_code)
            assert r.headers["www-authenticate"] == "Bearer" and r.json() == {"detail": "Authentication required"}
        assert c.get("/api/stats", headers={"authorization": "Bearer wrong-" + "z" * 30}).status_code == 401
        assert c.get("/api/stats", headers={"authorization": f"Bearer {TOKEN}"}).status_code == 200
        assert c.get("/api/stats", headers={"x-pja-token": TOKEN}).status_code == 200
        assert c.get(f"/api/stats?token={TOKEN}").status_code == 401  # never accepted from the URL


def test_guessing_the_token_is_throttled(clean_db):
    with api(PJA_API_TOKEN=TOKEN, PJA_RATE_LIMIT_ENABLED="true") as c:
        codes = [c.get("/api/stats", headers={"authorization": f"Bearer guess-{i:040d}"}).status_code for i in range(14)]
        assert codes[:10] == [401] * 10 and set(codes[10:]) == {429}
        r = c.get("/api/stats", headers={"authorization": "Bearer another-guess-" + "q" * 30})
        assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
        assert c.get("/api/stats", headers={"authorization": f"Bearer {TOKEN}"}).status_code == 200  # you still get in


def test_no_secret_ever_appears_in_an_api_response(clean_db):
    job_id = _job()
    with api(PJA_API_TOKEN=TOKEN, PJA_ATS_ACCOUNT_PASSWORD=ATS_SECRET) as c:
        auth = {"authorization": f"Bearer {TOKEN}"}
        for path in ["/api/profile", "/api/stats", "/api/agent", "/api/agent/runs", "/api/events", "/api/jobs",
                     f"/api/jobs/{job_id}", "/api/needs-you", "/api/questions/pending", "/api/answers/memory",
                     "/api/resumes", "/api/health"]:
            r = c.get(path, headers=auth)
            assert r.status_code == 200, path
            assert TOKEN not in r.text and ATS_SECRET not in r.text, path
        profile = c.get("/api/profile", headers=auth).json()
        assert not profile["path"].startswith("/")  # no absolute file-system path either
        assert all(not r["absolute_path"].startswith("/Users") for r in c.get("/api/resumes", headers=auth).json())


# ----------------------------------------------------------------------------------- response hygiene

def test_security_headers_on_every_response(clean_db):
    with api() as c:
        for r in (c.get("/api/stats"), c.get("/api/nope"), c.get("/api/stats", headers={"host": "evil.example"}),
                  c.post("/api/agent/mode", json={})):
            h = r.headers
            assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY"
            assert h["referrer-policy"] == "no-referrer" and "frame-ancestors 'none'" in h["content-security-policy"]
            assert "default-src 'none'" in h["content-security-policy"] and "camera=()" in h["permissions-policy"]
        assert c.get("/api/profile").headers["cache-control"] == "no-store"
        assert "strict-transport-security" not in c.get("/api/stats").headers  # plain http on loopback
    with api(PJA_HTTPS="true") as c:
        assert "max-age=31536000" in c.get("/api/stats").headers["strict-transport-security"]


def test_interactive_docs_are_off_unless_asked_for(clean_db):
    with api() as c:
        assert [c.get(p).status_code for p in ("/docs", "/redoc", "/openapi.json")] == [404, 404, 404]
    with api(PJA_API_DOCS="true") as c:
        assert [c.get(p).status_code for p in ("/docs", "/openapi.json")] == [200, 200]


def test_errors_do_not_leak_internals_or_echo_input(clean_db, monkeypatch):
    from app.api import routes_misc

    with api() as c:
        looks_like_a_key = "-".join(("sk", "A1b2C3d4" * 4))
        r = c.post("/api/answers/memory", json={"question": "", "answer": looks_like_a_key, "extra": "x"})
        assert r.status_code == 422 and looks_like_a_key not in r.text
        assert all(set(p) == {"loc", "msg", "type"} for p in r.json()["detail"])  # no "input" / "ctx" echo

        def boom(*_a, **_k):
            raise RuntimeError("database password is hunter2 at /Users/someone/secret/path.py")

        monkeypatch.setattr(routes_misc, "remember_answer", boom)
        try:
            client = TestClient(c.app, raise_server_exceptions=False)
            r = client.post("/api/answers/memory", json={"question": "Notice period?", "answer": "30 days"})
        finally:
            monkeypatch.undo()
        assert r.status_code == 500 and set(r.json()) == {"detail", "reference"}
        assert r.json()["detail"] == "Internal server error"
        assert "hunter2" not in r.text and "Traceback" not in r.text and "/Users" not in r.text


# ----------------------------------------------------------------------------------- input validation

@pytest.mark.parametrize("method,path,payload", [
    ("post", "/api/answers/memory", {"question": "q" * 2001, "answer": "a"}),
    ("post", "/api/answers/memory", {"question": "q", "answer": "a" * 5001}),
    ("post", "/api/answers/memory", {"question": "q", "answer": "a", "job_id": -1}),
    ("post", "/api/answers/memory", {"question": "q", "answer": "a", "is_admin": True}),  # unknown field
    ("post", "/api/jobs/evaluate", {"title": "", "description": "x"}),
    ("post", "/api/jobs/evaluate", {"title": "FE", "description": "x" * 60_001}),
    ("post", "/api/jobs/evaluate", {"title": "FE", "skills": ["s"] * 101}),
    ("post", "/api/jobs/1/action", {"action": "approve", "note": "n" * 1001}),
    ("post", "/api/needs-you/1/answers", {"answers": [{"question": "q", "answer": "a"}] * 101}),
    ("post", "/api/agent/start", {"mode": "auto", "repeat_minutes": -3}),
    ("post", "/api/agent/start", {"portals": ["naukri"] * 9}),
    ("post", "/api/agent/gate", {"response": "approve", "answers": {"k": "v" * 5001}}),
    ("put", "/api/profile/settings", {"personal": {"email": "attacker@example.com"}}),  # not an editable section
    ("put", "/api/profile/settings", {"resumes": [{"id": "x", "path": "/etc/passwd"}]}),
])
def test_out_of_range_and_unexpected_input_is_rejected(clean_db, method, path, payload):
    with api() as c:
        r = getattr(c, method)(path, json=payload)
        assert r.status_code == 422, (r.status_code, r.text[:200])


@pytest.mark.parametrize("path", [
    "/api/events?limit=-1", "/api/events?limit=0", "/api/events?limit=100000", "/api/agent/runs?limit=-1",
    "/api/jobs?page=0", "/api/jobs?page_size=100000", "/api/jobs?sort=id;drop table jobs",
    "/api/jobs?status=APPLIED';--", "/api/jobs?q=" + "x" * 201, "/api/jobs/0", "/api/jobs/-5",
    "/api/jobs/99999999999999999999",
])
def test_query_and_path_parameters_are_bounded(clean_db, path):
    with api() as c:
        assert c.get(path).status_code == 422, path


@pytest.mark.parametrize("payload", [
    "' OR '1'='1", "'; DROP TABLE jobs; --", "\" UNION SELECT name FROM sqlite_master --", "%", "_", "\\",
    "Robert'); DELETE FROM answer_memory;--", "1; ATTACH DATABASE '/tmp/x' AS x; --",
])
def test_sql_metacharacters_are_data_never_sql(clean_db, payload):
    _job("Frontend Engineer", "KitCo")
    _job("100% Remote React_Dev", "Percent & Co")
    with session_scope() as s:
        s.add(AnswerMemory(question="Notice period?", normalized_question="notice period", answer="30 days"))
    with api() as c:
        r = c.get("/api/jobs", params={"q": payload})
        assert r.status_code == 200
        if payload in {"%", "_"}:  # wildcards are matched literally: only the job that really contains them
            assert [j["title"] for j in r.json()["items"]] == ["100% Remote React_Dev"]
        else:
            assert r.json()["items"] == []
        saved = c.post("/api/answers/memory", json={"question": f"Why {payload}?", "answer": payload})
        assert saved.status_code == 200 and saved.json()["answer"] == payload  # stored verbatim, not mangled
        assert c.get("/api/jobs").json()["total"] == 2  # nothing was dropped, nothing was added
        assert len(c.get("/api/answers/memory").json()) == 2


@pytest.mark.parametrize("payload", [
    "<script>alert(document.cookie)</script>", "<img src=x onerror=alert(1)>", "javascript:alert(1)",
    "\"><svg/onload=alert(1)>", "{{constructor.constructor('alert(1)')()}}",
])
def test_markup_is_stored_as_text_and_served_as_json(clean_db, payload):
    """Job descriptions, AI text and your answers legitimately contain '<', '&' and quotes: nothing is stripped on
    the way in. The API only ever answers application/json (+ nosniff), and the dashboard renders text nodes."""
    job_id = _job(f"Dev {payload}", "X <b>Corp</b>")
    with api() as c:
        r = c.post("/api/answers/memory", json={"question": f"About {payload}", "answer": payload})
        assert r.status_code == 200 and r.json()["answer"] == payload
        for resp in (r, c.get(f"/api/jobs/{job_id}"), c.get("/api/jobs", params={"q": payload})):
            assert resp.headers["content-type"].startswith("application/json")
            assert resp.headers["x-content-type-options"] == "nosniff"
        assert c.get(f"/api/jobs/{job_id}").json()["title"] == f"Dev {payload}"
        assert c.post(f"/api/jobs/{job_id}/action", json={"action": payload}).status_code in {400, 422}


# ----------------------------------------------------------------------------------- files

@pytest.mark.parametrize("name", [
    "..%2F..%2Fconfig%2Fprofile.json", "%2e%2e%2f%2e%2e%2f.env", "....//....//jobs.db", "..", ".env",
    "/etc/passwd", "shot.png%00.html", "shot.svg", "shot.html%0d%0aSet-Cookie:x=1", "a" * 300 + ".png",
])
def test_screenshot_endpoint_serves_only_captures(clean_db, name):
    shots = get_settings().screenshots_path
    shots.mkdir(parents=True, exist_ok=True)
    (shots / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    (shots.parent / "private.png").write_bytes(b"\x89PNG")
    with api() as c:
        assert c.get(f"/api/screenshots/{name}").status_code in {404, 422}
        assert c.get("/api/screenshots/shot.png").status_code == 200


def test_a_saved_page_is_never_rendered_in_the_dashboard_origin(clean_db):
    shots = get_settings().screenshots_path
    shots.mkdir(parents=True, exist_ok=True)
    (shots / "failure.html").write_text("<script>alert(origin)</script>")
    with api() as c:
        r = c.get("/api/screenshots/failure.html")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
        assert r.headers["content-disposition"].startswith("attachment") and r.headers["x-content-type-options"] == "nosniff"
        assert "default-src 'none'" in r.headers["content-security-policy"]


def _upload(c: TestClient, name: str, content: bytes, resume_id: str = "sectest", **form: str):
    return c.post("/api/resumes", files={"file": (name, content, "application/octet-stream")},
                  data={"resume_id": resume_id, **form})


def test_resume_upload_accepts_documents_only(clean_db):
    pdf = b"%PDF-1.7\n% synthetic resume\n"
    with api() as c:
        ok = _upload(c, "../../../etc/My Resume (final).pdf", pdf)
        assert ok.status_code == 200, ok.text
        saved = Path(ok.json()["resume"]["path"])
        assert saved.name == "My-Resume-final.pdf" and ".." not in str(saved)  # the name cannot leave the folder
        target = saved if saved.is_absolute() else get_settings().resumes_path / saved.name
        assert target.read_bytes() == pdf and stat.S_IMODE(target.stat().st_mode) == 0o600
        for name, content in [("resume.pdf", b"<html><script>alert(1)</script></html>"),  # not a PDF
                              ("resume.pdf", b"MZ\x90\x00 executable"), ("resume.docx", b"%PDF-1.7 wrong type"),
                              ("resume.exe", pdf), ("resume.html", b"<html></html>"), ("resume.pdf", b"")]:
            r = _upload(c, name, content, "bad")
            assert r.status_code == 400, (name, r.status_code)
        assert _upload(c, "resume.pdf", pdf, "../../x").status_code == 400  # the id is a plain name
        assert _upload(c, "resume.pdf", pdf, "x" * 65).status_code == 422


def test_request_bodies_are_bounded(clean_db):
    with api(PJA_MAX_UPLOAD_BYTES="20000", PJA_MAX_REQUEST_BYTES="4096") as c:
        big_json = c.post("/api/answers/memory", json={"question": "q", "answer": "a" * 4500})
        assert big_json.status_code == 413 and "too large" in big_json.json()["detail"]
        assert _upload(c, "big.pdf", b"%PDF-1.7\n" + b"0" * 200_000).status_code == 413
        assert _upload(c, "small.pdf", b"%PDF-1.7\n" + b"0" * 5_000).status_code == 200
        lying = c.post("/api/answers/memory", content=b'{"question":"q","answer":"' + b"a" * 9000 + b'"}',
                       headers={"content-type": "application/json", "content-length": "notanumber"})
        assert lying.status_code in {400, 413}

        def chunks():  # no Content-Length at all: counted while it streams
            yield b'{"question":"q","answer":"'
            for _ in range(40):
                yield b"a" * 512
            yield b'"}'

        streamed = c.post("/api/answers/memory", content=chunks(), headers={"content-type": "application/json"})
        assert streamed.status_code == 413


# ----------------------------------------------------------------------------------- rate limits

def test_rate_rules_are_strictest_for_expensive_operations():
    assert rate_rule("POST", "/api/agent/start") == ("agent-start", 6, 60)
    assert rate_rule("POST", "/api/jobs/12/reevaluate")[0] == "evaluate" and rate_rule("POST", "/api/resumes")[0] == "upload"
    assert rate_rule("GET", "/api/health") == ("", 0, 0)
    read, write = rate_rule("GET", "/api/stats"), rate_rule("POST", "/api/answers/memory")
    assert read[1] > write[1] > rate_rule("POST", "/api/jobs/evaluate")[1] > rate_rule("POST", "/api/agent/start")[1]


def test_starting_the_agent_is_rate_limited_and_recovers(clean_db):
    with api(PJA_RATE_LIMIT_ENABLED="true") as c:
        codes = [c.post("/api/agent/start", json={"mode": "dry_run"}).status_code for _ in range(8)]
        assert codes[:6] == [200] * 6 and codes[6:] == [429, 429]
        blocked = c.post("/api/agent/start", json={"mode": "dry_run"})
        assert blocked.json() == {"detail": "Too many requests - slow down"} and int(blocked.headers["retry-after"]) >= 1
        assert c.get("/api/agent").status_code == 200  # reads and other controls are unaffected
        assert c.post("/api/agent/stop").status_code == 200
        limiter.reset()  # (the window passing)
        assert c.post("/api/agent/start", json={"mode": "dry_run"}).status_code == 200


def test_ai_evaluations_have_their_own_small_budget(clean_db):
    job_id = _job()
    with api(PJA_RATE_LIMIT_ENABLED="true") as c:
        plain = [c.post(f"/api/jobs/{job_id}/reevaluate").status_code for _ in range(8)]
        assert plain == [200] * 8  # rule-based scoring is cheap
        ai = [c.post(f"/api/jobs/{job_id}/reevaluate?use_ai=true").status_code for _ in range(8)]
        assert ai[:6] == [200] * 6 and ai[6:] == [429, 429]


def test_normal_dashboard_polling_is_never_limited(clean_db):
    with api(PJA_RATE_LIMIT_ENABLED="true") as c:
        # five open tabs for a minute: ~115 reads each
        codes = {c.get(p).status_code for _ in range(120) for p in ("/api/stats", "/api/agent", "/api/events",
                                                                     "/api/needs-you", "/api/jobs")}
        assert codes == {200}
        assert {c.get("/api/health").status_code for _ in range(300)} == {200}


def test_rate_limits_are_per_client(clean_db):
    assert limiter.check("t", "10.0.0.1", 2, 60) == 0 and limiter.check("t", "10.0.0.1", 2, 60) == 0
    assert limiter.check("t", "10.0.0.1", 2, 60) > 0
    assert limiter.check("t", "10.0.0.2", 2, 60) == 0  # somebody else's budget is untouched
    limiter.reset()
    with api(PJA_RATE_LIMIT_ENABLED="true") as c:  # a forged forwarding header does not buy a new budget
        codes = [c.post("/api/agent/start", json={"mode": "dry_run"}, headers={"x-forwarded-for": f"9.9.9.{i}"}).status_code
                 for i in range(8)]
        assert codes[6:] == [429, 429]


# ----------------------------------------------------------------------------------- logs, URLs, file modes

def test_credentials_are_redacted_from_logs(caplog):
    f = RedactingFilter([TOKEN, ATS_SECRET])
    for raw, gone in [
        (f"signing in with {ATS_SECRET} on workday", ATS_SECRET),
        (f"request had Authorization: Bearer {TOKEN}", TOKEN),
        ("cookie li_at=AQEDAT" + "x" * 40 + " sent", "AQEDAT" + "x" * 40),
        ("POST /login password=CorrectHorse9! ok", "CorrectHorse9!"),
        ("api_key: " + "abcd1234" + "efgh5678" + " loaded", "abcd1234" + "efgh5678"),
    ]:
        record = logging.LogRecord("t", logging.INFO, __file__, 1, raw, None, None)
        assert f.filter(record) and gone not in record.getMessage() and "[redacted]" in record.getMessage()
    plain = logging.LogRecord("t", logging.INFO, __file__, 1, "Job %s -> APPLIED (%s)", (12, "Applied on Instahyre"), None)
    f.filter(plain)
    assert plain.getMessage() == "Job 12 -> APPLIED (Applied on Instahyre)"  # ordinary lines are untouched


@pytest.mark.parametrize("url,ok", [
    ("https://careers.example.com/apply?id=1", True), ("http://localhost:8765/form.html", True),
    ("javascript:alert(1)", False), ("file:///etc/passwd", False), ("chrome://settings", False),
    ("data:text/html,<script>alert(1)</script>", False), ("ftp://example.com/x", False), ("//evil.example", False),
    ("about:blank", False), ("", False), (None, False), ("https://", False),
])
def test_only_web_addresses_count_as_urls(url, ok):
    assert is_web_url(url) is ok


async def test_the_agent_refuses_to_open_non_web_addresses(tmp_path):
    from app.browser.engine import BrowserEngine
    from app.browser.errors import NavigationFailed

    engine = BrowserEngine(headless=True, user_data_dir=tmp_path / "p", screenshot_dir=tmp_path / "s")
    for url in ("file:///etc/passwd", "javascript:alert(1)", "chrome://version", "data:text/html,x"):
        with pytest.raises(NavigationFailed, match="non-web address"):
            await engine.goto(url)  # refused before any browser is involved


@pytest.mark.parametrize("header,name", [
    ("localhost:8000", "localhost"), ("127.0.0.1", "127.0.0.1"), ("[::1]:8000", "::1"), ("EXAMPLE.com:443", "example.com"),
    ("a b", ""), ("user@host", ""), ("host/path", ""), ("[::1", ""), ("", ""),
])
def test_host_header_parsing(header, name):
    assert host_name(header) == name


def test_data_files_become_private(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "logs").mkdir(parents=True)
    (data / "screenshots").mkdir()
    files = [data / "jobs.db", data / "jobs.db-wal", data / "logs" / "agent.log", data / "screenshots" / "a.png",
             tmp_path / "profile.json", tmp_path / "resumes" / "cv.pdf"]
    (tmp_path / "resumes").mkdir()
    for f in files:
        f.write_bytes(b"x")
        f.chmod(0o644)
    for d in (data, data / "logs", tmp_path / "resumes"):
        d.chmod(0o755)
    for k, v in {"PJA_DATA_DIR": str(data), "PJA_PROFILE_PATH": str(tmp_path / "profile.json"),
                 "PJA_RESUMES_DIR": str(tmp_path / "resumes"), "PJA_BROWSER_PROFILE_DIR": str(data / "bp")}.items():
        monkeypatch.setenv(k, v)
    reset_settings_cache()
    try:
        changed = harden_permissions()
        assert all(stat.S_IMODE(f.stat().st_mode) == 0o600 for f in files), [oct(f.stat().st_mode) for f in files]
        assert all(stat.S_IMODE(d.stat().st_mode) == 0o700 for d in (data, data / "logs", tmp_path / "resumes"))
        assert len(changed) >= len(files) and harden_permissions() == []  # nothing left to tighten the second time
        assert all(f.read_bytes() == b"x" for f in files)  # nothing was deleted or rewritten
    finally:
        monkeypatch.undo()
        reset_settings_cache()
