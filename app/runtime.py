"""Process runtime: wires the FastAPI app, pre-render workers, and Telegram
bots into a single asyncio application.

The process runs one **independent stack per device** (its own ``Settings``,
``Database``, ``Services``, ``PreRenderWorker`` and ``Bot``), sharing only the
FastAPI app, one :class:`httpx.AsyncClient`, and one :class:`BrowserRenderer`
(Chromium serializes renders behind an internal lock, so it is safe to share
and saves a whole browser). Requests are routed to the right stack by the
``device_id`` in the URL path.

Combining everything in one event loop also avoids SQLite multi-process write
contention: each device's HTTP backend, renderer, and bot all share that
device's single database connection and ``Services`` instance.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Optional

import httpx
from aiogram import Bot, Dispatcher
from fastapi import FastAPI

from .api.routes import router as api_router
from .config import Settings, load_device_settings
from .db import Database
from .render.browser import BrowserRenderer
from .render.worker import PreRenderWorker
from .services import Services
from .telegram.handlers import build_dispatcher, setup_bot_commands
from .telegram.notify import Notifier

logger = logging.getLogger(__name__)


@dataclass
class DeviceRuntime:
    """All per-device state for one independent stack."""

    settings: Settings
    db: Database
    services: Services
    worker: Optional[PreRenderWorker] = None
    bot: Optional[Bot] = None
    dispatcher: Optional[Dispatcher] = None
    bot_task: Optional[asyncio.Task] = None
    monitor_task: Optional[asyncio.Task] = None


@dataclass
class AppContext:
    """Shared resources plus the per-device stacks keyed by ``device_id``."""

    http: httpx.AsyncClient
    stacks: dict[str, DeviceRuntime] = field(default_factory=dict)
    renderer: Optional[BrowserRenderer] = None


async def _start_stack(
    settings: Settings,
    *,
    http: httpx.AsyncClient,
    renderer: Optional[BrowserRenderer],
    run_worker: bool,
    run_bot: bool,
) -> DeviceRuntime:
    """Initialize one device's DB, services, worker, and bot."""
    settings.ensure_directories()

    db = Database(settings.database_path)
    await db.connect()

    services = Services(db=db, settings=settings, http=http)
    await services.seed()

    rt = DeviceRuntime(settings=settings, db=db, services=services)

    if run_worker and renderer is not None:
        worker = PreRenderWorker(
            db=db, settings=settings, renderer=renderer, http=http
        )
        worker.start()
        services.attach_worker(worker)
        rt.worker = worker

    if run_bot and settings.telegram_bot_token:
        bot = Bot(token=settings.telegram_bot_token)
        dispatcher = build_dispatcher(services)
        rt.bot = bot
        rt.dispatcher = dispatcher
        services.attach_notifier(Notifier(bot, settings.allowed_user_ids))
        with contextlib.suppress(Exception):
            await setup_bot_commands(bot)
        rt.bot_task = asyncio.create_task(
            dispatcher.start_polling(bot, handle_signals=False),
            name=f"telegram-polling-{settings.device_id}",
        )
        # Watch for the device going silent (offline) and alert once.
        rt.monitor_task = asyncio.create_task(
            services.run_offline_monitor(),
            name=f"offline-monitor-{settings.device_id}",
        )
        logger.info("Telegram bot polling started for %s", settings.device_id)
    elif run_bot:
        logger.warning(
            "TELEGRAM_BOT_TOKEN not set for %s; Telegram bot disabled",
            settings.device_id,
        )

    return rt


async def _stop_stack(rt: DeviceRuntime) -> None:
    """Tear down one device's background tasks and database connection."""
    if rt.monitor_task is not None:
        rt.monitor_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await rt.monitor_task
    if rt.bot_task is not None and rt.dispatcher is not None:
        with contextlib.suppress(Exception):
            await rt.dispatcher.stop_polling()
        rt.bot_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await rt.bot_task
    if rt.bot is not None:
        with contextlib.suppress(Exception):
            await rt.bot.session.close()
    if rt.worker is not None:
        await rt.worker.stop()
    await rt.db.close()


async def startup(
    settings: Optional[Settings] = None,
    *,
    run_worker: bool = True,
    run_bot: bool = True,
) -> AppContext:
    """Initialize all shared resources and start every device's background tasks.

    When ``settings`` is provided the process runs that single stack (used by
    tests / ``create_app(settings=...)``); otherwise stacks are loaded from
    :func:`load_device_settings` (env, incl. optional ``DEVICE_<n>_*`` vars).
    """
    device_settings = [settings] if settings is not None else load_device_settings()

    http = httpx.AsyncClient(headers={"User-Agent": "m5color-trmnl/0.1"})

    renderer: Optional[BrowserRenderer] = None
    if run_worker:
        renderer = BrowserRenderer()
        await renderer.start()

    ctx = AppContext(http=http, renderer=renderer)

    for ds in device_settings:
        rt = await _start_stack(
            ds,
            http=http,
            renderer=renderer,
            run_worker=run_worker,
            run_bot=run_bot,
        )
        ctx.stacks[ds.device_id] = rt

    logger.info(
        "Started %d device stack(s): %s",
        len(ctx.stacks),
        ", ".join(ctx.stacks) or "-",
    )
    return ctx


async def shutdown(ctx: AppContext) -> None:
    """Tear down every device stack, then the shared resources."""
    for rt in ctx.stacks.values():
        with contextlib.suppress(Exception):
            await _stop_stack(rt)
    if ctx.renderer is not None:
        await ctx.renderer.stop()
    with contextlib.suppress(Exception):
        await ctx.http.aclose()
    logger.info("Shutdown complete")


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    """Build the combined FastAPI application (API + workers + bots)."""

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        ctx = await startup(settings)
        app.state.ctx = ctx
        try:
            yield
        finally:
            await shutdown(ctx)

    app = FastAPI(title="m5color-trmnl backend", version="0.1.0", lifespan=lifespan)
    app.include_router(api_router)
    return app
