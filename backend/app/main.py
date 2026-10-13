"""FastAPI application (dashboard API)."""

from __future__ import annotations

import logging
import os
import sys
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import routes_agent, routes_jobs, routes_misc, routes_profile
from app.config import ConfigError, get_settings
from app.db import init_db
from app.logging_config import setup_logging
from app.profile import get_profile_store
from app.security import SecurityMiddleware, harden_permissions

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    os.umask(0o077)  # everything this process creates (database, logs, uploads) is private to your account
    setup_logging("api")
    init_db()
    tightened = harden_permissions()
    if tightened:
        log.info("Restricted %d data file(s) / folder(s) to your account only", len(tightened))
    store = get_profile_store()
    store.ensure_exists()
    try:
        store.load()
    except Exception as exc:  # keep the API up so the dashboard can show the problem
        log.error("Profile could not be loaded: %s", exc)
    settings = get_settings()
    log.info("API ready (hosts: %s; token: %s; docs: %s; rate limits: %s)", ", ".join(settings.allowed_host_list),
             "required" if settings.api_token else "not set (loopback only)", "on" if settings.api_docs else "off",
             "on" if settings.rate_limit_enabled else "off")
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    docs = settings.api_docs
    app = FastAPI(title="Personal Job Assistant", version="1.0.0", lifespan=lifespan, debug=False,
                  docs_url="/docs" if docs else None, redoc_url="/redoc" if docs else None,
                  openapi_url="/openapi.json" if docs else None)
    # CORS only matters for a dashboard that calls the API directly; the bundled one goes through its own server.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "X-PJA-Token"],
        max_age=600,
    )
    app.add_middleware(SecurityMiddleware, settings=settings)  # added last = runs first

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # what is wrong and where - without echoing the submitted values back
        problems = [{"loc": [str(p) for p in e.get("loc", [])], "msg": str(e.get("msg", "invalid")),
                     "type": str(e.get("type", ""))} for e in exc.errors()[:20]]
        return JSONResponse({"detail": problems}, status_code=422)

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        ref = uuid.uuid4().hex[:12]
        log.exception("Unhandled error %s on %s %s", ref, request.method, request.url.path)
        return JSONResponse({"detail": "Internal server error", "reference": ref}, status_code=500)

    app.include_router(routes_misc.router)
    app.include_router(routes_profile.router)
    app.include_router(routes_jobs.router)
    app.include_router(routes_agent.router)
    return app


try:
    app = create_app()
except ConfigError as exc:  # a clear message instead of a traceback, and no half-configured server
    sys.stderr.write(f"\n{exc}\n\nThe API was not started.\n")
    raise SystemExit(2) from None
