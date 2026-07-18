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


class _AccessLogFilter(logging.Filter):
    """Drop uvicorn access-log lines for high-frequency polling endpoints.

    The web panel polls ``/status`` every few seconds and refreshes the frame
    previews, which would otherwise flood the console with one ``200 OK`` line
    per request. Only the panel's own (``/api/ui/``) polling is dropped -- real
    actions (POSTs for skip/favorite/mode) and the device's ``/api/status``
    heartbeat are left untouched. Uvicorn logs the path as ``record.args[2]``.
    """

    _NOISY = ("/status", "/current.png", "/preview.png")

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3:
            path = str(args[2]).split("?", 1)[0]
            if path.startswith("/api/ui/") and path.endswith(self._NOISY):
                return False
        return True


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs every outbound request at INFO ("HTTP Request: GET ... 200 OK").
    # Presence polling makes that constant noise -> only surface warnings.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # Filter the panel's polling requests out of uvicorn's access log. Added to
    # the logger object before uvicorn's dictConfig runs; that config leaves
    # existing filters in place (disable_existing_loggers is False).
    logging.getLogger("uvicorn.access").addFilter(_AccessLogFilter())


configure_logging()

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
