"""Logging setup: console + rotating file per process (api.log / agent.log)."""

from __future__ import annotations

import logging
import logging.handlers

from app.config import get_settings

_CONFIGURED: set[str] = set()

FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"


def setup_logging(process_name: str = "api") -> None:
    if process_name in _CONFIGURED:
        return
    settings = get_settings()
    settings.ensure_dirs()
    root = logging.getLogger()
    root.setLevel(settings.log_level.upper())

    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
               for h in root.handlers):
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter(FORMAT, "%H:%M:%S"))
        root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        settings.logs_path / f"{process_name}.log", maxBytes=5_000_000, backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(FORMAT))
    root.addHandler(file_handler)

    for noisy in ("httpx", "httpcore", "asyncio", "urllib3", "multipart"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _CONFIGURED.add(process_name)
