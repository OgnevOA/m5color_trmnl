"""Standalone Telegram bot entrypoint.

In the default deployment the bot runs inside the combined backend process
(see ``server.py`` / :mod:`app.runtime`). This entrypoint is provided for
running / testing the bot on its own. It shares the same ``app/`` services and
SQLite database, and also starts a pre-render worker so queued content gets
rendered.

Usage:
    python bot.py
"""

from __future__ import annotations

import asyncio
import logging

from app.config import get_settings
from app.runtime import shutdown, startup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("bot")


async def _run() -> None:
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required to run the bot.")

    ctx = await startup(settings, run_worker=True, run_bot=True)
    logger.info("Bot is running. Press Ctrl+C to stop.")
    try:
        if ctx.bot_task is not None:
            await ctx.bot_task
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        await shutdown(ctx)


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
