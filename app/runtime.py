"""Process runtime: wires the FastAPI app, pre-render worker, and Telegram bot
into a single asyncio application.

The combined process avoids SQLite multi-process write contention: the HTTP
backend, the background renderer, and the bot all share one event loop, one
database connection, and one :class:`~app.services.Services` instance.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import Optional

import httpx
from aiogram import Bot, Dispatcher
from fastapi import FastAPI

from .api.routes import router as api_router
from .config import Settings, get_settings
from .db import Database
from .render.browser import BrowserRenderer
from .render.worker import PreRenderWorker
from .services import Services
from .telegram.handlers import build_dispatcher, setup_bot_commands

logger = logging.getLogger(__name__)


@dataclass
class AppContext:
    settings: Settings
    db: Database
    http: httpx.AsyncClient
    services: Services
    renderer: Optional[BrowserRenderer] = None
    worker: Optional[PreRenderWorker] = None
    bot: Optional[Bot] = None
    dispatcher: Optional[Dispatcher] = None
    bot_task: Optional[asyncio.Task] = None


async def startup(
    settings: Optional[Settings] = None,
    *,
    run_worker: bool = True,
    run_bot: bool = True,
) -> AppContext:
    """Initialize all shared resources and start background tasks."""
    settings = settings or get_settings()
    settings.ensure_directories()

    db = Database(settings.database_path)
    await db.connect()

    http = httpx.AsyncClient(headers={"User-Agent": "m5color-trmnl/0.1"})
    services = Services(db=db, settings=settings, http=http)
    await services.seed()

    ctx = AppContext(settings=settings, db=db, http=http, services=services)

    if run_worker:
        renderer = BrowserRenderer()
        await renderer.start()
        worker = PreRenderWorker(db=db, settings=settings, renderer=renderer)
        worker.start()
        services.attach_worker(worker)
        ctx.renderer = renderer
        ctx.worker = worker

    if run_bot and settings.telegram_bot_token:
        bot = Bot(token=settings.telegram_bot_token)
        dispatcher = build_dispatcher(services)
        ctx.bot = bot
        ctx.dispatcher = dispatcher
        with contextlib.suppress(Exception):
            await setup_bot_commands(bot)
        ctx.bot_task = asyncio.create_task(
            dispatcher.start_polling(bot, handle_signals=False),
            name="telegram-polling",
        )
        logger.info("Telegram bot polling started")
    elif run_bot:
        logger.warning("TELEGRAM_BOT_TOKEN not set; Telegram bot disabled")

    return ctx


async def shutdown(ctx: AppContext) -> None:
    """Tear down background tasks and shared resources."""
    if ctx.bot_task is not None and ctx.dispatcher is not None:
        with contextlib.suppress(Exception):
            await ctx.dispatcher.stop_polling()
        ctx.bot_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await ctx.bot_task
    if ctx.bot is not None:
        with contextlib.suppress(Exception):
            await ctx.bot.session.close()
    if ctx.worker is not None:
        await ctx.worker.stop()
    if ctx.renderer is not None:
        await ctx.renderer.stop()
    with contextlib.suppress(Exception):
        await ctx.http.aclose()
    await ctx.db.close()
    logger.info("Shutdown complete")


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    """Build the combined FastAPI application (API + worker + bot)."""

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        ctx = await startup(settings)
        app.state.ctx = ctx
        app.state.services = ctx.services
        try:
            yield
        finally:
            await shutdown(ctx)

    app = FastAPI(title="m5color-trmnl backend", version="0.1.0", lifespan=lifespan)
    app.include_router(api_router)
    return app
