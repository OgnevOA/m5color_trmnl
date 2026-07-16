"""Standalone Telegram bot entrypoint.

In the default deployment the bot(s) run inside the combined backend process
(see ``server.py`` / :mod:`app.runtime`). This entrypoint is provided for
running / testing the bot(s) on their own. It shares the same ``app/`` services
and SQLite database(s), and also starts a pre-render worker per device so
queued content gets rendered. All configured device stacks are started (one bot
per device); with no ``DEVICE_<n>_*`` env vars this is just the single
env-configured device.

Usage:
    python bot.py
"""

from __future__ import annotations

import asyncio
import logging

from app.config import load_device_settings
from app.runtime import shutdown, startup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("bot")


async def _run() -> None:
    if not any(ds.telegram_bot_token for ds in load_device_settings()):
        raise SystemExit("No TELEGRAM_BOT_TOKEN configured for any device.")

    ctx = await startup(run_worker=True, run_bot=True)
    bot_tasks = [
        rt.bot_task for rt in ctx.stacks.values() if rt.bot_task is not None
    ]
    logger.info("Bot(s) running (%d). Press Ctrl+C to stop.", len(bot_tasks))
    try:
        if bot_tasks:
            await asyncio.gather(*bot_tasks)
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
