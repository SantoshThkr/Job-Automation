"""Protection for the dashboard API of a single-user, local-first application.

Who may talk to this API: you, from the dashboard on this machine. There are no user accounts (one profile, one
database), so the job here is to make sure every request really comes from that dashboard:

    Host allow-list     a request addressed to any other name is refused. This is the defence against DNS rebinding,
                        where a web page you visit makes its own domain resolve to 127.0.0.1 and then reads the API.
    cross-site check    a request another web site makes from your browser (CSRF) is refused: `Sec-Fetch-Site:
                        cross-site`, or an `Origin` that is not the dashboard's.
    API token           optional on loopback, mandatory otherwise (config.py refuses to start without it): a shared
                        secret the dashboard *server* adds to each request; compared in constant time.
    size limits         request bodies are bounded (JSON 1 MB, resume upload 10 MB by default).
    rate limits         per client and kind of operation; stricter for anything that starts the browser agent, calls
                        the local model or writes files. In-memory: exact for the single API process this project
                        runs (`make api`); with several workers each would count on its own.
    response headers    nosniff, no framing, no referrer, no caching of personal data, a deny-all CSP for API output.

The browser agent does not use this API (it shares the database), so nothing here can slow down or block automation.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from app.config import Settings, get_settings, is_loopback_host

log = logging.getLogger(__name__)

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")

# (methods, path pattern, bucket, max requests, per seconds) - first match wins
RATE_RULES: list[tuple[set[str], re.Pattern[str], str, int, int]] = [
    ({"GET", "HEAD"}, re.compile(r"^/api/health$"), "", 0, 0),  # liveness probe: never limited
    ({"POST"}, re.compile(r"^/api/agent/start$"), "agent-start", 6, 60),  # launches the browser agent
    ({"POST", "PUT"}, re.compile(r"^/api/agent/(?:pause|resume|stop|mode|gate)$"), "agent-control", 60, 60),
    ({"POST"}, re.compile(r"^/api/jobs/(?:evaluate|\d+/reevaluate)$"), "evaluate", 30, 60),
    ({"POST"}, re.compile(r"^/api/resumes$"), "upload", 10, 60),
    ({"GET"}, re.compile(r"^/api/ai/status$"), "ai-status", 30, 60),  # probes the local model each time
]
WRITE_LIMIT = ("write", 120, 60)
READ_LIMIT = ("read", 1200, 60)  # one open dashboard tab polls about 115 times a minute
AI_LIMIT = ("ai", 6, 60)  # local-model calls requested through the API (see require_ai_budget)
AUTH_FAILURE_LIMIT = ("auth-failure", 10, 300)


class RateLimiter:
    """Sliding-window counter per (bucket, client)."""

    def __init__(self) -> None:
        self._hits: dict[tuple[str, str], deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, bucket: str, client: str, limit: int, window_s: int, *, count: bool = True) -> float:
        """0.0 when the request is within the limit, else the seconds to wait before retrying."""
        if not bucket or limit <= 0:
            return 0.0
        now = time.monotonic()
        with self._lock:
            hits = self._hits.setdefault((bucket, client), deque())
            while hits and now - hits[0] >= window_s:
                hits.popleft()
            if len(hits) >= limit:
                return max(1.0, window_s - (now - hits[0]))
            if count:
                hits.append(now)
            if len(self._hits) > 4096:  # many distinct clients: drop the idle ones
                for key in [k for k, v in self._hits.items() if not v]:
                    del self._hits[key]
        return 0.0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()


def host_name(host_header: str) -> str:
    """'localhost:8000' -> 'localhost', '[::1]:8000' -> '::1' (lower case; '' when malformed)."""
    h = (host_header or "").strip().lower()
    if not h or any(c in h for c in " /\\@,"):
        return ""
    if h.startswith("["):
        end = h.find("]")
        return h[1:end] if end > 0 else ""
    if h.count(":") > 1:  # a bare IPv6 address
        return h
    return h.rsplit(":", 1)[0] if ":" in h else h


def rate_rule(method: str, path: str) -> tuple[str, int, int]:
    for methods, pattern, bucket, limit, window in RATE_RULES:
        if method in methods and pattern.search(path):
            return bucket, limit, window
    return READ_LIMIT if method in SAFE_METHODS else WRITE_LIMIT


def security_headers(path: str, settings: Settings) -> list[tuple[bytes, bytes]]:
    headers = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Cross-Origin-Resource-Policy": "same-site",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    }
    if path.startswith("/api"):
        headers["Cache-Control"] = "no-store"  # profile data, answers and screenshots stay out of caches
    if not (settings.api_docs and path.startswith(DOCS_PATHS)):  # (Swagger UI loads its script from a CDN)
        headers["Content-Security-Policy"] = ("default-src 'none'; frame-ancestors 'none'; base-uri 'none'; "
                                              "form-action 'none'")
    if settings.https:
        headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()]


class SecurityMiddleware:
    """One pass over every request, before routing: see the module docstring for what is checked and why."""

    def __init__(self, app: Callable[..., Awaitable[None]], settings: Settings | None = None) -> None:
        self.app = app
        self.settings = settings or get_settings()
        s = self.settings
        self.hosts = set(s.allowed_host_list)
        if not is_loopback_host(s.api_host):  # deliberately exposed (token enforced): the bind address is a valid name
            self.hosts.add(s.api_host.strip("[]").lower())
        self.origins = {o.lower() for o in s.cors_origin_list}
        self.token = s.api_token.strip()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        s = self.settings
        method, path = scope["method"].upper(), scope.get("path", "")
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        client = (scope.get("client") or ("unknown", 0))[0]  # the socket's peer; forwarding headers are not trusted
        started = False
        too_large = False  # the body outgrew its limit while streaming: whatever the app answers, we answer 413
        limit = s.max_upload_bytes + 64 * 1024 if (method, path) == ("POST", "/api/resumes") else s.max_request_bytes

        async def reject(status: int, detail: str, extra: dict[str, str] | None = None) -> None:
            nonlocal started
            started = True
            body = json.dumps({"detail": detail}).encode()
            head = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
            head += [(k.lower().encode(), v.encode()) for k, v in (extra or {}).items()]
            await send({"type": "http.response.start", "status": status, "headers": head + security_headers(path, s)})
            await send({"type": "http.response.body", "body": body})

        async def send_with_headers(message: Message) -> None:
            nonlocal started
            if too_large:
                if not started:
                    await reject(413, f"Request body too large (limit {limit // 1024} KB)")
                return  # the app's own answer to a body it never fully received is dropped
            if message["type"] == "http.response.start":
                started = True
                extra = security_headers(path, s)
                present = {k for k, _ in extra}
                message["headers"] = [(k, v) for k, v in message.get("headers", []) if k.lower() not in present] + extra
            await send(message)

        # 1. addressed to us? (DNS rebinding)
        if host_name(headers.get("host", "")) not in self.hosts:
            log.warning("refused request for unexpected host (%s %s)", method, path)
            await reject(400, "Invalid Host header")
            return
        if method == "OPTIONS":  # CORS preflight: answered by the CORS middleware, carries no credentials
            await self.app(scope, receive, send_with_headers)
            return

        # 2. made by another web site? (CSRF / cross-origin reads)
        origin = headers.get("origin", "").lower().rstrip("/")
        own = {f"http://{headers.get('host', '').lower()}", f"https://{headers.get('host', '').lower()}"}
        if headers.get("sec-fetch-site", "").lower() == "cross-site" or (origin and origin not in self.origins | own):
            log.warning("refused cross-site request (%s %s)", method, path)
            await reject(403, "Cross-site requests are not allowed")
            return

        # 3. authenticated? (only when a token is configured; always when the API is not on loopback)
        if self.token and path.startswith("/api") and path != "/api/health":
            supplied = headers.get("x-pja-token", "")
            auth = headers.get("authorization", "")
            if not supplied and auth.lower().startswith("bearer "):
                supplied = auth[7:].strip()
            if not supplied or not hmac.compare_digest(supplied.encode(), self.token.encode()):
                bucket, max_failures, window = AUTH_FAILURE_LIMIT
                wait = limiter.check(bucket, client, max_failures, window) if s.rate_limit_enabled else 0
                if wait:
                    await reject(429, "Too many failed attempts - try again later", {"Retry-After": str(int(wait))})
                    return
                await reject(401, "Authentication required", {"WWW-Authenticate": "Bearer"})
                return

        # 4. bounded?
        declared = headers.get("content-length", "")
        if declared and (not declared.isdigit() or int(declared) > limit):
            await reject(413, f"Request body too large (limit {limit // 1024} KB)")
            return
        if s.rate_limit_enabled:
            bucket, max_hits, window = rate_rule(method, path)
            wait = limiter.check(bucket, client, max_hits, window)
            if wait:
                log.warning("rate limit reached for '%s' (%s %s)", bucket, method, path)
                await reject(429, "Too many requests - slow down", {"Retry-After": str(int(wait))})
                return

        received = 0

        async def bounded_receive() -> Message:
            nonlocal received, too_large
            if too_large:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:  # a chunked body that did not state (or lied about) its length
                    too_large = True
                    return {"type": "http.disconnect"}  # the app stops reading; nothing of it is processed
            return message

        try:
            await self.app(scope, bounded_receive, send_with_headers)
        except Exception:
            if not too_large:
                raise
        if too_large and not started:
            await reject(413, f"Request body too large (limit {limit // 1024} KB)")


def require_ai_budget(client: str = "local") -> None:
    """For handlers that call the local model on request: a stricter budget than ordinary writes."""
    from fastapi import HTTPException

    if not get_settings().rate_limit_enabled:
        return
    wait = limiter.check(AI_LIMIT[0], client, AI_LIMIT[1], AI_LIMIT[2])
    if wait:
        raise HTTPException(429, "Too many AI evaluations - try again shortly", headers={"Retry-After": str(int(wait))})


# ----------------------------------------------------------------------------------------- log redaction

_SECRET_SHAPES = re.compile(
    r"(?i)(\b(?:authorization|x-pja-token)\s*[:=]\s*(?:bearer\s+)?)\S+"
    r"|(\b(?:password|passwd|secret|token|api[_-]?key|li_at|jsessionid|session[_-]?id)\s*[=:]\s*)[^\s,;&'\"]+")


class RedactingFilter(logging.Filter):
    """Keep credentials out of log files: the configured secrets by value, and anything shaped like a credential
    assignment (password=..., Authorization: Bearer ..., li_at=...)."""

    def __init__(self, secrets: list[str] | None = None) -> None:
        super().__init__()
        self.secrets = sorted({s for s in (secrets or []) if len(s) >= 4}, key=len, reverse=True)

    def scrub(self, text: str) -> str:
        for secret in self.secrets:
            if secret in text:
                text = text.replace(secret, "[redacted]")
        return _SECRET_SHAPES.sub(lambda m: (m.group(1) or m.group(2)) + "[redacted]", text)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a malformed log call must still be logged
            return True
        scrubbed = self.scrub(message)
        if scrubbed != message:
            record.msg, record.args = scrubbed, None
        return True


# ----------------------------------------------------------------------------------------- URLs and files

def is_web_url(url: str | None) -> bool:
    """Only http(s) addresses are followed or shown as links: a scraped 'apply' link could be file:, javascript:,
    chrome: or data: just as well."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return False
    return parts.scheme in {"http", "https"} and bool(parts.netloc)


def harden_permissions(settings: Settings | None = None) -> list[str]:
    """Make the files that hold your data readable by your account only (0700 directories, 0600 files): profile,
    .env, database and its backups, logs, screenshots, resumes, the browser profile's top directory.
    Access for you is unchanged. Returns what was tightened; never raises."""
    import os
    import stat
    from pathlib import Path

    from app.config import PROJECT_ROOT

    s = settings or get_settings()
    changed: list[str] = []

    def tighten(path: Path, mode: int) -> None:
        try:
            if path.is_symlink() or not path.exists() or path.stat().st_uid != os.getuid():
                return
            if stat.S_IMODE(path.stat().st_mode) & 0o077:
                path.chmod(mode)
                changed.append(str(path))
        except OSError as exc:
            log.debug("could not tighten %s: %s", path, exc)

    for d in (s.data_path, s.logs_path, s.screenshots_path, s.browser_profile_path, s.resumes_path):
        tighten(d, 0o700)
    files = [s.profile_file, PROJECT_ROOT / ".env"]
    for d, patterns in ((s.data_path, ("*.db", "*.db-wal", "*.db-shm", "*.db-journal", "*.lock")),
                        (s.logs_path, ("*",)), (s.screenshots_path, ("*",)), (s.resumes_path, ("*",))):
        if d.is_dir():
            for pattern in patterns:
                files += [p for p in d.glob(pattern) if p.is_file()]
    for f in files:
        tighten(f, 0o600)
    return changed
