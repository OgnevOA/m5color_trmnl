"""Now Playing mode: show the poster of a Home Assistant media_player.

On each refill it asks Home Assistant for the configured media_player's current
artwork and displays it full-screen, reusing the photo render path. When nothing
is playing (or the same title is still showing) it returns ``None`` so the device
holds its current display instead of re-flashing the e-ink panel every wake.
"""

from __future__ import annotations

import logging
from typing import Optional

from .. import home_assistant
from .base import ContentItem, ContentKind, Mode, ModeContext

logger = logging.getLogger(__name__)

#: Last poster signature shown per media_player entity. Module-level so it
#: survives across the fresh Mode instances created on each generate() call
#: (the process is long-lived). Lost on restart -> one harmless re-render.
_LAST_SIG: dict[str, str] = {}


class NowPlayingMode(Mode):
    name = "now_playing"
    description = "Poster of whatever is playing in the living room."
    periodic = True
    #: Cover art is continuous-tone: let the device dither it.
    epd_mode = "quality"

    async def generate(self, ctx: ModeContext) -> Optional[ContentItem]:
        cfg = ctx.settings
        if not cfg.now_playing_configured:
            return ContentItem(
                kind=ContentKind.text,
                title="Now Playing",
                text=(
                    "Set HOME_ASSISTANT_URL, HOME_ASSISTANT_TOKEN and "
                    "HOME_ASSISTANT_MEDIA_PLAYER_ENTITY to enable now playing."
                ),
            )

        result = await home_assistant.media_player_poster(ctx.http, cfg)
        if result is None:
            return None  # nothing playing / no art / error -> hold display.

        image_bytes, signature = result
        entity = cfg.home_assistant_media_player_entity
        if _LAST_SIG.get(entity) == signature:
            return None  # same title still showing -> don't re-render.

        _LAST_SIG[entity] = signature
        return ContentItem(
            kind=ContentKind.image,
            title="Now Playing",
            image_bytes=image_bytes,
        )
