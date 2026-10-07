"""Application settings loaded from environment variables / the project-level .env file.

All relative paths are resolved against the project root (the folder that contains
``backend/``, ``frontend/`` and ``config/``) so commands work from any directory.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


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
    api_port: int = 8000
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

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
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3:4b"
    ollama_timeout_s: float = 90.0
    ai_min_confidence: float = 0.75

    # Agent
    default_mode: str = "dry_run"
    log_level: str = "INFO"

    # Portal base URLs (overridable so adapters can run against local mock sites in tests)
    naukri_base_url: str = "https://www.naukri.com"
    linkedin_base_url: str = "https://www.linkedin.com"
    instahyre_base_url: str = "https://www.instahyre.com"

    # Testing helpers
    agent_spawn_enabled: bool = Field(default=True, description="Allow the API to spawn the agent process")

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
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def ensure_dirs(self) -> None:
        for d in (self.data_path, self.screenshots_path, self.logs_path, self.resumes_path,
                  self.browser_profile_path):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
