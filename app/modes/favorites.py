"""Favorites mode: replay a random previously-starred picture.

The user stars frames from the web panel; each is copied (overlay-free) into a
durable per-device store. This mode shows a random one on each wake. The actual
picture selection lives in :meth:`app.services.Services.generate_for_active_mode`
(it needs DB + filesystem access, like the artist collage modifier); this class
is the registry entry and the empty-set fallback.
"""

from __future__ import annotations

from typing import Optional

from .base import ContentItem, ContentKind, Mode, ModeContext


class FavoritesMode(Mode):
    name = "favorites"
    description = "Randomly display pictures you have favorited."
    #: Periodic: pick a fresh random favorite on each refill.
    periodic = True
    #: Favorites are photos/artwork (continuous tone) -> dither for quality.
    epd_mode = "quality"

    async def generate(self, ctx: ModeContext) -> Optional[ContentItem]:
        # Reached only when there are no favorites yet (Services short-circuits
        # to a random favorite otherwise).
        return ContentItem(
            kind=ContentKind.text,
            title="Favorites",
            text="No favorites yet. Star an image in the web panel to add it.",
        )
