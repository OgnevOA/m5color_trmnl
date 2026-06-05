"""Outbound Telegram notifications (server -> user).

A thin wrapper around the aiogram :class:`~aiogram.Bot` used by the service
layer to push proactive alerts (low battery, device offline, ...). Sending is
best-effort: a failure to reach one recipient never raises into the caller.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from aiogram import Bot

logger = logging.getLogger(__name__)


class Notifier:
    """Fan-out alerts to the configured Telegram recipients."""

    def __init__(self, bot: Bot, recipient_ids: Iterable[int]) -> None:
        self._bot = bot
        # In private chats the chat_id equals the user_id.
        self._recipients = [int(rid) for rid in recipient_ids]

    async def send(self, text: str) -> None:
        for chat_id in self._recipients:
            try:
                await self._bot.send_message(chat_id, text)
            except Exception as exc:
                logger.warning("notify %s failed: %s", chat_id, exc)
