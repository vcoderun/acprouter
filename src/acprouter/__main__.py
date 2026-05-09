from __future__ import annotations as _annotations

import asyncio
import logging
import os
from pathlib import Path

import dotenv

from .settings import AppSettings
from .telegram_gateway import run_telegram_gateway

__all__ = ("main",)


def _cwd_dotenv_path() -> Path:
    return Path.cwd() / ".env"


def _load_environment() -> None:
    dotenv.load_dotenv(dotenv_path=_cwd_dotenv_path(), override=False)


def _configure_logging() -> None:
    raw_level = os.environ.get("ACPROUTER_LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, raw_level, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main() -> None:
    _load_environment()
    _configure_logging()
    settings = AppSettings.from_env()
    asyncio.run(run_telegram_gateway(settings))


if __name__ == "__main__":
    main()
