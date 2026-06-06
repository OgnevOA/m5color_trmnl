"""Plain text mode: content is supplied by the user via Telegram messages."""

from __future__ import annotations

from typing import Optional

from .base import ContentItem, ContentKind, Mode, ModeContext


class PlainTextMode(Mode):
    name = "plain_text"
    description = "Display plain text sent by the user."
    periodic = False
    epd_mode = "text"

    async def generate(self, ctx: ModeContext) -> Optional[ContentItem]:
        # Nothing to auto-generate; this mode renders user-provided text.
        return ContentItem(
            kind=ContentKind.text,
            title="Plain Text",
            text="Send me any text message and it will appear here.",
        )
