"""aiogram Telegram bot: commands and content input.

The bot shares the same :class:`~app.services.Services` instance as the HTTP
backend and worker, so commands operate on the exact same state.
"""

from __future__ import annotations

import io
import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, TelegramObject

from ..auth import is_user_allowed
from ..modes.registry import available_modes
from ..services import Services

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "TRMNL e-ink control bot\n\n"
    "/status - show system status\n"
    "/interval N - set polling interval to N minutes\n"
    "/mode NAME - set the active mode\n"
    "/queue - show queue status\n"
    "/clear - clear pending queue entries\n"
    "/next - skip to / generate the next item\n"
    "/night on|off|status - control night mode\n"
    "/help - show this help\n\n"
    "Send any text to display it. Send a photo to show it on the device."
)


class AuthMiddleware(BaseMiddleware):
    """Drop updates from users who are not authorized."""

    def __init__(self, services: Services) -> None:
        self._services = services

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return None
        allowed = await is_user_allowed(
            self._services.db, self._services.settings, user.id
        )
        if not allowed:
            if isinstance(event, Message):
                await event.answer("Not authorized.")
            logger.info("Rejected Telegram user %s", user.id)
            return None
        return await handler(event, data)


def build_router(services: Services) -> Router:
    router = Router(name="trmnl")

    @router.message(CommandStart())
    async def cmd_start(message: Message) -> None:
        snap = await services.get_status_snapshot()
        await message.answer(
            f"Welcome! Active mode: {snap.mode}, interval: "
            f"{snap.interval_minutes} min.\n\n{HELP_TEXT}"
        )

    @router.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        await message.answer(HELP_TEXT)

    @router.message(Command("status"))
    async def cmd_status(message: Message) -> None:
        s = await services.get_status_snapshot()
        last_seen = s.last_seen.strftime("%Y-%m-%d %H:%M:%S") if s.last_seen else "never"
        battery = f"{s.battery_percent:.0f}%" if s.battery_percent is not None else "?"
        await message.answer(
            "System status\n"
            f"- Device: {s.device_id}\n"
            f"- Mode: {s.mode}\n"
            f"- Interval: {s.interval_minutes} min\n"
            f"- Night mode: {'on' if s.night_mode_enabled else 'off'} "
            f"({'night now' if s.is_night_now else 'day now'})\n"
            f"- Last update: {last_seen}\n"
            f"- Last wake reason: {s.last_wake_reason or '?'}\n"
            f"- Last image: {s.last_image_id or '-'}\n"
            f"- Battery: {battery}\n"
            f"- Queue: {s.queue_ready} ready / {s.queue_pending} pending"
        )

    @router.message(Command("interval"))
    async def cmd_interval(message: Message) -> None:
        parts = (message.text or "").split()
        if len(parts) < 2 or not parts[1].isdigit():
            await message.answer("Usage: /interval N  (minutes, e.g. /interval 60)")
            return
        minutes = int(parts[1])
        await services.set_interval(minutes)
        await message.answer(f"Polling interval set to {max(1, minutes)} minutes.")

    @router.message(Command("mode"))
    async def cmd_mode(message: Message) -> None:
        parts = (message.text or "").split()
        if len(parts) < 2:
            await message.answer(
                "Usage: /mode NAME\nAvailable: " + ", ".join(available_modes())
            )
            return
        name = parts[1].strip()
        known, item_id = await services.select_mode(name)
        if not known:
            await message.answer(
                f"Unknown mode '{name}'. Available: " + ", ".join(available_modes())
            )
        elif item_id is not None:
            await message.answer(
                f"Mode set to {name}. Generating the first item (rendering)..."
            )
        else:
            await message.answer(
                f"Mode set to {name}. Send content to display it."
            )

    @router.message(Command("queue"))
    async def cmd_queue(message: Message) -> None:
        s = await services.get_status_snapshot()
        await message.answer(
            f"Queue: {s.queue_ready} ready, {s.queue_pending} pending."
        )

    @router.message(Command("clear"))
    async def cmd_clear(message: Message) -> None:
        cleared = await services.clear_queue()
        await message.answer(f"Cleared {cleared} queued item(s).")

    @router.message(Command("next"))
    async def cmd_next(message: Message) -> None:
        item_id = await services.generate_for_active_mode()
        if item_id is None:
            await message.answer("A next item is already queued (or nothing to generate).")
        else:
            await message.answer("Generating next item for the active mode...")

    @router.message(Command("night"))
    async def cmd_night(message: Message) -> None:
        parts = (message.text or "").split()
        arg = parts[1].lower() if len(parts) > 1 else "status"
        if arg == "on":
            await services.set_night_mode(True)
            await message.answer("Night mode enabled (23:00-06:30).")
        elif arg == "off":
            await services.set_night_mode(False)
            await message.answer("Night mode disabled.")
        else:
            s = await services.get_status_snapshot()
            await message.answer(
                f"Night mode is {'on' if s.night_mode_enabled else 'off'} "
                f"({'night now' if s.is_night_now else 'day now'}). Window: 23:00-06:30."
            )

    @router.message(F.photo)
    async def on_photo(message: Message, bot: Bot) -> None:
        photo = message.photo[-1]
        data = await _download(bot, photo.file_id)
        await services.enqueue_user_image(data, suffix=".jpg")
        await message.answer("Image queued for rendering.")

    @router.message(F.document)
    async def on_document(message: Message, bot: Bot) -> None:
        doc = message.document
        if not (doc.mime_type or "").startswith("image/"):
            await message.answer("Please send an image.")
            return
        data = await _download(bot, doc.file_id)
        await services.enqueue_user_image(data, suffix=".png")
        await message.answer("Image queued for rendering.")

    @router.message(F.text)
    async def on_text(message: Message) -> None:
        text = (message.text or "").strip()
        if not text:
            return
        await services.enqueue_user_text(text)
        await message.answer("Text queued for rendering.")

    return router


async def _download(bot: Bot, file_id: str) -> bytes:
    file = await bot.get_file(file_id)
    buf = io.BytesIO()
    await bot.download_file(file.file_path, buf)
    return buf.getvalue()


def build_dispatcher(services: Services) -> Dispatcher:
    dispatcher = Dispatcher()
    middleware = AuthMiddleware(services)
    dispatcher.message.middleware(middleware)
    dispatcher.include_router(build_router(services))
    return dispatcher
