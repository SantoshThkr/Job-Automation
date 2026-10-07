"""Portal adapter registry."""

from __future__ import annotations

from app.browser.engine import BrowserEngine
from app.config import get_settings
from app.portals.base import PortalAdapter
from app.portals.instahyre import InstahyreAdapter
from app.portals.linkedin import LinkedInAdapter
from app.portals.naukri import NaukriAdapter

PORTALS: dict[str, type[PortalAdapter]] = {
    "naukri": NaukriAdapter,
    "linkedin": LinkedInAdapter,
    "instahyre": InstahyreAdapter,
}


def base_url_for(name: str) -> str:
    s = get_settings()
    return {"naukri": s.naukri_base_url, "linkedin": s.linkedin_base_url, "instahyre": s.instahyre_base_url}[name]


def create_adapter(name: str, engine: BrowserEngine, base_url: str | None = None) -> PortalAdapter | None:
    cls = PORTALS.get(name)
    if cls is None:
        return None
    return cls(engine, base_url or base_url_for(name))
