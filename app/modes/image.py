"""Image mode: content is supplied by the user as photos via Telegram."""

from __future__ import annotations

from typing import Optional

from .base import ContentItem, ContentKind, Mode, ModeContext


class ImageMode(Mode):
    name = "image"
    description = "Display images sent by the user."
    periodic = False
    #: Photos are dithered server-side to the exact panel palette, so the
    #: device just packs them nearest-color (no second on-panel dither).
    epd_mode = "fastest"

    async def generate(self, ctx: ModeContext) -> Optional[ContentItem]:
        return ContentItem(
            kind=ContentKind.text,
            title="Image Mode",
            text="Send me a photo and it will be shown on the display.",
        )
