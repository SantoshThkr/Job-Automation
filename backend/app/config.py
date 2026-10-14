"""Application settings loaded from environment variables / the project-level .env file.

All relative paths are resolved against the project root (the folder that contains
``backend/``, ``frontend/`` and ``config/``) so commands work from any directory.
"""

from __future__ import annotations

import ipaddress
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# What /api/health answers as "service". The dashboard checks it before it forwards anything: port 8000 is a popular
# default, and another program listening on it (a Docker container of another project did, on 2026-10-13) would
# otherwise receive the dashboard's requests - and its API token - whenever this API is not running.
SERVICE_NAME = "personal-job-assistant"

# settings whose value must never appear in a log line or an error message
SECRET_FIELDS = ("ats_account_password", "api_token")
MIN_TOKEN_LENGTH = 24


class ConfigError(RuntimeError):
    """The configuration (environment / .env) is missing something or is not usable. The message names the
    variable and what is wrong - never a secret's value."""


def is_loopback_host(host: str) -> bool:
    """True for names / addresses that only this machine can reach."""
    h = (host or "").strip().strip("[]").lower()
    if h in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PJA_",
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Paths
    data_dir: Path = Path("data")
    profile_path: Path = Path("config/profile.json")
    resumes_dir: Path = Path("resumes")
    database_url: str = ""  # defaults to sqlite in data_dir

    # API
    api_host: str = "127.0.0.1"
    # 8010, not the common 8000: another program on 8000 (on this Mac, another project's container) would answer the
    # dashboard whenever this API is not running
    api_port: int = Field(default=8010, ge=1, le=65535)
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    # API protection (see app/security.py). The API is meant for this machine only.
    # Host names the API answers to: a request for any other name (DNS rebinding) is refused.
    allowed_hosts: str = "localhost,127.0.0.1,::1,host.docker.internal"
    # Shared secret for the API. Optional while the API listens on loopback; REQUIRED (24+ characters) as soon as
    # PJA_API_HOST is anything else. The dashboard server adds it to every request; browsers never see it.
    api_token: str = ""
    api_docs: bool = False  # /docs, /redoc and /openapi.json (off unless you ask for them)
    rate_limit_enabled: bool = True
    max_request_bytes: int = Field(default=1_048_576, ge=1024, le=64 * 1024 * 1024)  # JSON bodies
    max_upload_bytes: int = Field(default=10 * 1024 * 1024, ge=1024, le=64 * 1024 * 1024)  # resume uploads
    https: bool = False  # true only when the API is served over HTTPS (adds Strict-Transport-Security)

    # Browser
    browser_headless: bool = False
    browser_channel: str = ""  # "" = bundled Chromium, "chrome" = installed Google Chrome
    browser_slow_mo_ms: int = 0
    browser_profile_dir: Path = Path("data/browser-profile")
    nav_timeout_ms: int = 30_000
    action_timeout_ms: int = 10_000
    human_delay_ms_min: int = 250
    human_delay_ms_max: int = 900
    login_wait_timeout_s: int = 600
    review_wait_timeout_s: int = 900

    # AI (local Ollama)
    ai_enabled: bool = True
    ollama_url: str = "http://127.0.0.1:11434"  # (not "localhost": that may resolve to another server on ::1)
    ollama_model: str = "qwen3:4b"
    ollama_timeout_s: float = 90.0
    # How long the model stays in memory after a request (it takes ~3 GB while loaded). Ollama's own default is
    # 5 minutes, which keeps it loaded for a whole run; a short value frees the memory between applications.
    ollama_keep_alive: str = "30s"
    ai_min_confidence: float = 0.75
    # Memory safety: before each application the run checks the Mac's memory state (pressure level, swap, free
    # disk). Under pressure the local AI is not loaded; when memory is exhausted the run waits this long for it to
    # recover and then stops, leaving the remaining jobs queued. false = never check.
    memory_guard: bool = True
    memory_wait_s: int = 120
    # At the start of a run, recent jobs that were skipped for a hard requirement under *other* preferences are
    # evaluated again (rules only). false = leave them; `python -m app.cli reevaluate --skipped --dry` previews it.
    reconsider_skipped: bool = True
    ai_model_memory_mb: int = 3200  # what loading the local model takes; it is not loaded with less headroom (0 = no check)
    browser_disk_cache_mb: int = 100  # cap of the agent browser's HTTP cache on disk (0 = Chrome's default)

    # Agent
    default_mode: str = "dry_run"
    log_level: str = "INFO"
    # Auto Apply: how long to wait for you (CAPTCHA, OTP, unknown question, sign-in) before parking the
    # job as "needs you" and moving on. 0 = never wait in Auto Apply.
    auto_human_wait_s: int = 180
    notify: bool = True  # macOS notification whenever the agent needs you

    # Navigator (generic browser agent for company sites / unknown application systems)
    navigator_enabled: bool = True
    navigator_use_ai: bool = True  # ask the local model when the page is not understood by the rules
    navigator_max_steps: int = 45
    navigator_max_minutes: float = 15
    # Employer career-site accounts (Workday, SuccessFactors, Taleo, iCIMS...). Optional: with a password
    # the agent signs in with your profile email, and may create an account when a site requires one
    # (never in Dry Run). Without it, the agent pauses and asks you to sign in.
    ats_account_password: str = ""
    # Sign-ins stored per site in the macOS Keychain (python -m app.cli site-login set <site>) are looked up before
    # the general password above. false = never ask the Keychain.
    site_login_keychain: bool = True
    # "Sign in with Google" on a career site: the assistant opens Google's own page and waits for you there (it
    # types nothing on it). false = such buttons are left alone, as before.
    google_signin: bool = True
    ats_create_accounts: bool = True

    # Portal base URLs (overridable so adapters can run against local mock sites in tests)
    naukri_base_url: str = "https://www.naukri.com"
    linkedin_base_url: str = "https://www.linkedin.com"
    instahyre_base_url: str = "https://www.instahyre.com"

    # Testing helpers
    agent_spawn_enabled: bool = Field(default=True, description="Allow the API to spawn the agent process")

    # ------------------------------------------------------------------ validation (fail early, fail clearly)
    @field_validator("default_mode")
    @classmethod
    def _mode(cls, v: str) -> str:
        if v not in {"dry_run", "review", "auto"}:
            raise ValueError("must be dry_run, review or auto")
        return v

    @field_validator("log_level")
    @classmethod
    def _log_level(cls, v: str) -> str:
        if v.upper() not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("must be DEBUG, INFO, WARNING, ERROR or CRITICAL")
        return v.upper()

    @field_validator("ollama_url", "naukri_base_url", "linkedin_base_url", "instahyre_base_url")
    @classmethod
    def _http_url(cls, v: str) -> str:
        parts = urlsplit(v.strip())
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError("must be an http(s) URL")
        return v.strip().rstrip("/")

    @field_validator("cors_origins")
    @classmethod
    def _origins(cls, v: str) -> str:
        for origin in (o.strip() for o in v.split(",") if o.strip()):
            parts = urlsplit(origin)
            if origin == "*" or "*" in origin:
                raise ValueError("a wildcard origin is not allowed - list the dashboard's origin(s)")
            if parts.scheme not in {"http", "https"} or not parts.netloc or parts.path not in {"", "/"} or parts.query:
                raise ValueError("each entry must be an origin such as http://localhost:3000")
        return v

    @field_validator("allowed_hosts")
    @classmethod
    def _hosts(cls, v: str) -> str:
        hosts = [h.strip() for h in v.split(",") if h.strip()]
        if not hosts:
            raise ValueError("list at least one host name (localhost,127.0.0.1)")
        for h in hosts:
            if "*" in h or "/" in h or not re.fullmatch(r"[A-Za-z0-9.\-:\[\]]{1,253}", h):
                raise ValueError("host names only (no wildcard, scheme or path), e.g. localhost,127.0.0.1")
        return v

    @model_validator(mode="after")
    def _exposure(self) -> Settings:
        token = self.api_token.strip()
        if token and len(token) < MIN_TOKEN_LENGTH:
            raise ValueError(f"PJA_API_TOKEN must be at least {MIN_TOKEN_LENGTH} characters "
                             "(generate one: python3 -c 'import secrets; print(secrets.token_urlsafe(32))')")
        if not is_loopback_host(self.api_host) and not token:
            raise ValueError("PJA_API_HOST is not a loopback address, so the API would be reachable from other "
                             "machines without any authentication. Set PJA_API_TOKEN (24+ characters) or use "
                             "PJA_API_HOST=127.0.0.1")
        if self.human_delay_ms_min > self.human_delay_ms_max:
            raise ValueError("PJA_HUMAN_DELAY_MS_MIN must not be larger than PJA_HUMAN_DELAY_MS_MAX")
        return self

    def resolve(self, p: Path) -> Path:
        return p if p.is_absolute() else (PROJECT_ROOT / p).resolve()

    @property
    def data_path(self) -> Path:
        return self.resolve(self.data_dir)

    @property
    def profile_file(self) -> Path:
        return self.resolve(self.profile_path)

    @property
    def resumes_path(self) -> Path:
        return self.resolve(self.resumes_dir)

    @property
    def browser_profile_path(self) -> Path:
        return self.resolve(self.browser_profile_dir)

    @property
    def screenshots_path(self) -> Path:
        return self.data_path / "screenshots"

    @property
    def logs_path(self) -> Path:
        return self.data_path / "logs"

    @property
    def db_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.data_path / 'jobs.db'}"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip().rstrip("/") for o in self.cors_origins.split(",") if o.strip()]

    @property
    def allowed_host_list(self) -> list[str]:
        return [h.strip().strip("[]").lower() for h in self.allowed_hosts.split(",") if h.strip()]

    @property
    def secret_values(self) -> list[str]:
        """Configured secrets (for log redaction); never log or return these."""
        return [v for v in (getattr(self, f, "") for f in SECRET_FIELDS) if v]

    def ensure_dirs(self) -> None:
        for d in (self.data_path, self.screenshots_path, self.logs_path, self.resumes_path,
                  self.browser_profile_path):
            d.mkdir(parents=True, exist_ok=True)


def describe_validation_error(exc: ValidationError) -> str:
    """One line per problem, naming the environment variable. The offending value is shown only for settings that
    are not secrets (and only its first 60 characters)."""
    lines = []
    for err in exc.errors():
        field = str(err["loc"][0]) if err.get("loc") else ""
        msg = err.get("msg", "invalid value").removeprefix("Value error, ")
        if not field:  # model-level rule: the message already names the variable(s)
            lines.append(f"  - {msg}")
            continue
        shown = ""
        if field not in SECRET_FIELDS and not isinstance(err.get("input"), (dict, list)):
            shown = f" (got {str(err.get('input'))[:60]!r})"
        lines.append(f"  - PJA_{field.upper()}: {msg}{shown}")
    return "Invalid configuration (environment variables / .env):\n" + "\n".join(lines)


@lru_cache
def get_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        raise ConfigError(describe_validation_error(exc)) from None


def reset_settings_cache() -> None:
    get_settings.cache_clear()
