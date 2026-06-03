"""Entrypoint for the combined backend process.

Runs the FastAPI HTTP API, the background pre-render worker, and (if a bot
token is configured) the Telegram bot -- all in one asyncio process.

Usage:
    python server.py
    # or for development with reload:
    uvicorn app.runtime:create_app --factory --reload
"""

from __future__ import annotations

import logging

import uvicorn

from app.config import get_settings
from app.runtime import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

app = create_app()


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
