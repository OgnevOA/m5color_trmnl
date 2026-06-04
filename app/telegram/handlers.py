"""aiogram Telegram bot: commands, inline-keyboard UI, and content input.

The bot shares the same :class:`~app.services.Services` instance as the HTTP
backend and worker, so commands operate on the exact same state.

UI: in addition to slash commands, an inline-keyboard menu (callback buttons)
provides point-and-click control. Inline buttons are used (not a reply
keyboard) so button presses are never mistaken for content to display.
"""

from __future__ import annotations

import io
import logging
import re
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from ..auth import is_user_allowed
from ..modes.registry import available_modes
from ..services import Services

logger = logging.getLogger(__name__)

CB = "ui:"  # callback-data prefix for all menu buttons

HELP_TEXT = (
    "TRMNL e-ink control bot\n\n"
    "Use the buttons below, or these commands:\n"
    "/menu - open the control panel\n"
    "/status - show system status\n"
    "/interval N - set polling interval to N minutes\n"
    "/mode NAME - set the active mode\n"
    "/queue - show queue status\n"
    "/clear - clear pending queue entries\n"
    "/next - skip to / generate the next item\n"
    "/night on|off|status - control night mode\n\n"
    "Send any text to display it. Send a photo to show it on the device.\n"
    "Prefix a message with 'qr:' to render it as a QR code "
    "(e.g. 'qr: https://example.com')."
)

#: Matches a leading 'qr:' prefix (case-insensitive) and captures the rest.
QR_PREFIX = re.compile(r"^\s*qr\s*:\s*(.+)$", re.IGNORECASE | re.DOTALL)

BOT_COMMANDS = [
    BotCommand(command="menu", description="Open the control panel"),
    BotCommand(command="status", description="Show system status"),
    BotCommand(command="mode", description="Set the active mode"),
    BotCommand(command="interval", description="Set polling interval (minutes)"),
    BotCommand(command="next", description="Generate / skip to the next item"),
    BotCommand(command="queue", description="Show queue status"),
    BotCommand(command="clear", description="Clear the pending queue"),
    BotCommand(command="night", description="Control night mode"),
    BotCommand(command="help", description="Show help"),
]

MODE_LABELS = {
    "plain_text": "Text",
    "image": "Image",
    "qr": "QR",
    "random_friends_quote": "Friends",
    "random_office_quote": "The Office",
    "random_scrubs_quote": "Scrubs",
    "random_xkcd": "XKCD",
}

INTERVAL_CHOICES = [15, 30, 60, 120, 240]


# --------------------------------------------------------------------------- #
# Keyboards
# --------------------------------------------------------------------------- #
def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=CB + data)


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("Status", "status"), _btn("Queue", "queue")],
            [_btn("Mode", "modes"), _btn("Interval", "interval")],
            [_btn("Next", "next"), _btn("Clear queue", "clear")],
            [_btn("Night mode", "night"), _btn("Help", "help")],
        ]
    )


def modes_menu(current: str) -> InlineKeyboardMarkup:
    buttons: list[InlineKeyboardButton] = []
    for name in available_modes():
        label = MODE_LABELS.get(name, name)
        mark = "\u2705 " if name == current else ""
        buttons.append(_btn(f"{mark}{label}", f"mode:{name}"))
    # Two buttons per row for a compact grid.
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append([_btn("\u2190 Back", "home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def interval_menu(current: int) -> InlineKeyboardMarkup:
    row: list[InlineKeyboardButton] = []
    for n in INTERVAL_CHOICES:
        mark = "\u2705 " if n == current else ""
        label = f"{mark}{n}m" if n < 60 else f"{mark}{n // 60}h"
        row.append(_btn(label, f"int:{n}"))
    return InlineKeyboardMarkup(
        inline_keyboard=[row, [_btn("\u2190 Back", "home")]]
    )


def night_menu(enabled: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _btn(("\u2705 " if enabled else "") + "On", "night:on"),
                _btn(("\u2705 " if not enabled else "") + "Off", "night:off"),
            ],
            [_btn("\u2190 Back", "home")],
        ]
    )


# --------------------------------------------------------------------------- #
# Text builders (shared by commands and callbacks)
# --------------------------------------------------------------------------- #
async def status_text(services: Services) -> str:
    s = await services.get_status_snapshot()
    if s.last_seen is not None:
        tz = ZoneInfo(services.settings.timezone)
        last_seen = s.last_seen.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S %Z")
    else:
        last_seen = "never"
    battery = f"{s.battery_percent:.0f}%" if s.battery_percent is not None else "?"
    return (
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


async def queue_text(services: Services) -> str:
    s = await services.get_status_snapshot()
    return f"Queue: {s.queue_ready} ready, {s.queue_pending} pending."


# --------------------------------------------------------------------------- #
# Auth middleware (covers messages and callback queries)
# --------------------------------------------------------------------------- #
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
            logger.info("Rejected Telegram user %s", user.id)
            if isinstance(event, Message):
                await event.answer("Not authorized.")
            elif isinstance(event, CallbackQuery):
                await event.answer("Not authorized.", show_alert=True)
            return None
        return await handler(event, data)


async def _safe_edit(
    callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup
) -> None:
    """Edit the menu message, ignoring 'message is not modified' errors."""
    if callback.message is None:
        return
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


# --------------------------------------------------------------------------- #
# Router
# --------------------------------------------------------------------------- #
def build_router(services: Services) -> Router:
    router = Router(name="trmnl")

    async def menu_header() -> str:
        s = await services.get_status_snapshot()
        return (
            "TRMNL control panel\n"
            f"Mode: {MODE_LABELS.get(s.mode, s.mode)}  |  "
            f"Interval: {s.interval_minutes}m  |  "
            f"Night: {'on' if s.night_mode_enabled else 'off'}\n"
            f"Queue: {s.queue_ready} ready / {s.queue_pending} pending"
        )

    # -- Commands -------------------------------------------------------- #
    @router.message(CommandStart())
    @router.message(Command("menu"))
    async def cmd_menu(message: Message) -> None:
        await message.answer(await menu_header(), reply_markup=main_menu())

    @router.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        await message.answer(HELP_TEXT, reply_markup=main_menu())

    @router.message(Command("status"))
    async def cmd_status(message: Message) -> None:
        await message.answer(await status_text(services), reply_markup=main_menu())

    @router.message(Command("queue"))
    async def cmd_queue(message: Message) -> None:
        await message.answer(await queue_text(services), reply_markup=main_menu())

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
        await _apply_mode(message, parts[1].strip())

    async def _apply_mode(message: Message, name: str) -> None:
        known, item_id = await services.select_mode(name)
        if not known:
            await message.answer(
                f"Unknown mode '{name}'. Available: " + ", ".join(available_modes())
            )
        elif item_id is not None:
            await message.answer(f"Mode set to {name}. Generating the first item...")
        else:
            await message.answer(f"Mode set to {name}. Send content to display it.")

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

    # -- Inline-keyboard callbacks --------------------------------------- #
    @router.callback_query(F.data.startswith(CB))
    async def on_callback(callback: CallbackQuery) -> None:
        action = (callback.data or "")[len(CB):]
        s = await services.get_status_snapshot()

        if action == "home":
            await _safe_edit(callback, await menu_header(), main_menu())
            await callback.answer()
        elif action == "status":
            await _safe_edit(callback, await status_text(services), main_menu())
            await callback.answer()
        elif action == "queue":
            await _safe_edit(callback, await queue_text(services), main_menu())
            await callback.answer("Refreshed")
        elif action == "modes":
            await _safe_edit(callback, "Select a mode:", modes_menu(s.mode))
            await callback.answer()
        elif action == "interval":
            await _safe_edit(
                callback, "Select polling interval:", interval_menu(s.interval_minutes)
            )
            await callback.answer()
        elif action == "night":
            await _safe_edit(
                callback, "Night mode (window 23:00-06:30):",
                night_menu(s.night_mode_enabled),
            )
            await callback.answer()
        elif action == "next":
            item_id = await services.generate_for_active_mode()
            await callback.answer(
                "Generating next item..." if item_id is not None
                else "Already queued / nothing to generate"
            )
            await _safe_edit(callback, await menu_header(), main_menu())
        elif action == "clear":
            cleared = await services.clear_queue()
            await callback.answer(f"Cleared {cleared} item(s)")
            await _safe_edit(callback, await menu_header(), main_menu())
        elif action.startswith("mode:"):
            name = action.split(":", 1)[1]
            known, item_id = await services.select_mode(name)
            if not known:
                await callback.answer("Unknown mode", show_alert=True)
            elif item_id is not None:
                await callback.answer(f"{MODE_LABELS.get(name, name)}: generating...")
            else:
                await callback.answer(f"{MODE_LABELS.get(name, name)}: send content")
            await _safe_edit(callback, await menu_header(), main_menu())
        elif action.startswith("int:"):
            minutes = int(action.split(":", 1)[1])
            await services.set_interval(minutes)
            await callback.answer(f"Interval set to {minutes} min")
            await _safe_edit(callback, await menu_header(), main_menu())
        elif action.startswith("night:"):
            enable = action.split(":", 1)[1] == "on"
            await services.set_night_mode(enable)
            await callback.answer(f"Night mode {'enabled' if enable else 'disabled'}")
            await _safe_edit(
                callback, "Night mode (window 23:00-06:30):", night_menu(enable)
            )
        elif action == "help":
            await _safe_edit(callback, HELP_TEXT, main_menu())
            await callback.answer()
        else:
            await callback.answer()

    # -- Content input --------------------------------------------------- #
    @router.message(F.photo)
    async def on_photo(message: Message, bot: Bot) -> None:
        photo = message.photo[-1]
        data = await _download(bot, photo.file_id)
        _, started_new = await services.enqueue_user_image(
            data, suffix=".jpg", media_group_id=message.media_group_id
        )
        # Only reply once per album (on the photo that starts the carousel).
        if started_new:
            await message.answer(
                "Switched to image mode. Send several photos in one message "
                "to carousel them; they'll cycle until you send something new."
            )

    @router.message(F.document)
    async def on_document(message: Message, bot: Bot) -> None:
        doc = message.document
        if not (doc.mime_type or "").startswith("image/"):
            await message.answer("Please send an image.")
            return
        data = await _download(bot, doc.file_id)
        _, started_new = await services.enqueue_user_image(
            data, suffix=".png", media_group_id=message.media_group_id
        )
        if started_new:
            await message.answer("Switched to image mode. Image queued for rendering.")

    @router.message(F.text)
    async def on_text(message: Message) -> None:
        text = (message.text or "").strip()
        if not text:
            return
        qr_match = QR_PREFIX.match(text)
        if qr_match:
            payload = qr_match.group(1).strip()
            if not payload:
                await message.answer("Nothing to encode. Try 'qr: HELLO'.")
                return
            await services.enqueue_qr(payload)
            await message.answer("Switched to QR mode. QR code queued for rendering.")
            return
        await services.enqueue_user_text(text)
        await message.answer("Switched to text mode. Text queued for rendering.")

    return router


async def _download(bot: Bot, file_id: str) -> bytes:
    file = await bot.get_file(file_id)
    buf = io.BytesIO()
    await bot.download_file(file.file_path, buf)
    return buf.getvalue()


async def setup_bot_commands(bot: Bot) -> None:
    """Replace the bot's command menu (the blue '/' button in Telegram).

    This overwrites whatever was previously registered via setMyCommands,
    including stale commands from older bot versions.
    """
    await bot.set_my_commands(BOT_COMMANDS)


def build_dispatcher(services: Services) -> Dispatcher:
    dispatcher = Dispatcher()
    middleware = AuthMiddleware(services)
    dispatcher.message.middleware(middleware)
    dispatcher.callback_query.middleware(middleware)
    dispatcher.include_router(build_router(services))
    return dispatcher
