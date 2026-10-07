"""FastAPI application (dashboard API)."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import routes_agent, routes_jobs, routes_misc, routes_profile
from app.config import get_settings
from app.db import init_db
from app.logging_config import setup_logging
from app.profile import get_profile_store

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    setup_logging("api")
    init_db()
    store = get_profile_store()
    store.ensure_exists()
    try:
        store.load()
    except Exception as exc:  # keep the API up so the dashboard can show the problem
        log.error("Profile could not be loaded: %s", exc)
    log.info("API ready")
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Personal Job Assistant", version="1.0.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(routes_misc.router)
    app.include_router(routes_profile.router)
    app.include_router(routes_jobs.router)
    app.include_router(routes_agent.router)
    return app


app = create_app()
