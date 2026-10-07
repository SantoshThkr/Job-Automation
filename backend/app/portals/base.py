"""Portal adapter interface. Each job portal lives in its own module with its own selectors."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from app.browser.engine import BrowserEngine
from app.browser.errors import LoginRequired
from app.engine.context import ApplyContext, GateResult, HumanGate
from app.portals.types import ApplyResult, JobPosting

log = logging.getLogger(__name__)


@dataclass
class SearchQuery:
    keywords: str
    location: str = ""
    experience_years: float | None = None
    posted_within_days: int | None = None
    max_pages: int = 3
    max_jobs: int = 50
    easy_apply_only: bool = False
    extra: dict = field(default_factory=dict)


class PortalAdapter(ABC):
    name: str = "portal"
    display_name: str = "Portal"

    def __init__(self, engine: BrowserEngine, base_url: str) -> None:
        self.engine = engine
        self.base_url = base_url.rstrip("/")

    # ---- session -----------------------------------------------------
    @property
    @abstractmethod
    def login_url(self) -> str: ...

    @abstractmethod
    async def is_logged_in(self, navigate: bool = True) -> bool:
        """With navigate=False only inspect the current page (used while the user is logging in)."""

    async def ensure_logged_in(self, gate: HumanGate | None, timeout_s: int | None = None) -> None:
        """Open the portal; if the session expired, wait for the user to log in in the browser window."""
        if await self.is_logged_in():
            return
        if gate is None:
            raise LoginRequired(self.name)
        await self.engine.goto(self.login_url)
        res = await gate.wait("login", f"Please log in to {self.display_name} in the browser window",
                              page=self.engine.page, detect_done=lambda: self.is_logged_in(navigate=False),
                              timeout_s=timeout_s)
        if res not in (GateResult.RESOLVED, GateResult.APPROVE) or not await self.is_logged_in():
            raise LoginRequired(self.name, f"Not logged in to {self.display_name}")

    # ---- discovery ---------------------------------------------------
    @abstractmethod
    def search(self, query: SearchQuery) -> AsyncIterator[JobPosting]: ...

    @abstractmethod
    async def fetch_details(self, job: JobPosting) -> JobPosting: ...

    # ---- application -------------------------------------------------
    @abstractmethod
    async def apply(self, job: JobPosting, ctx: ApplyContext) -> ApplyResult: ...
