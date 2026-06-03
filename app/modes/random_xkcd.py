"""Random XKCD mode: fetch a comic and display it as an image."""

from __future__ import annotations

import logging
import random
from typing import Optional

from .base import ContentItem, ContentKind, Mode, ModeContext

logger = logging.getLogger(__name__)

_LATEST_URL = "https://xkcd.com/info.0.json"
_NUM_URL = "https://xkcd.com/{num}/info.0.json"


class RandomXkcdMode(Mode):
    name = "random_xkcd"
    description = "Display a random XKCD comic."

    async def generate(self, ctx: ModeContext) -> Optional[ContentItem]:
        try:
            latest = (await ctx.http.get(_LATEST_URL, timeout=15)).json()
            max_num = int(latest["num"])
            num = random.randint(1, max_num)
            meta = (
                await ctx.http.get(_NUM_URL.format(num=num), timeout=15)
            ).json()
            img_url = meta["img"]
            img_resp = await ctx.http.get(img_url, timeout=20)
            img_resp.raise_for_status()
            return ContentItem(
                kind=ContentKind.image,
                title=meta.get("safe_title", "XKCD"),
                image_bytes=img_resp.content,
            )
        except Exception as exc:  # network or parsing failure
            logger.warning("random_xkcd generation failed: %s", exc)
            return ContentItem(
                kind=ContentKind.text,
                title="XKCD",
                text="Could not fetch a comic right now.",
            )
