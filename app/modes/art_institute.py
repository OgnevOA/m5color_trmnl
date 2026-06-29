"""Art Institute of Chicago mode: show a random public-domain artwork.

On each refill it picks a random public-domain artwork from the AIC API and
returns the IIIF image as an ``image`` content item. Like ``random_xkcd`` it
performs its network I/O off the device wake path and degrades to a text
fallback if a request fails. No API key is required.
"""

from __future__ import annotations

import logging
import random
from typing import Optional

from .base import ContentItem, ContentKind, Mode, ModeContext

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://api.artic.edu/api/v1/artworks/search"
_IIIF_BASE = "https://www.artic.edu/iiif/2"
# AIC asks API clients to identify themselves with a contact handle, and the
# image CDN gatekeeps requests with a missing/library User-Agent.
_HEADERS = {
    "User-Agent": "m5color_trmnl/1.0 (e-ink art frame)",
    "AIC-User-Agent": "m5color_trmnl e-ink frame (github.com/oleg/m5color_trmnl)",
}
# Records fetched per random page. A batch lets us skip null-image_id rows
# (a chunk of the collection has no public IIIF image) without many round-trips.
_PAGE_SIZE = 10
# The AIC search API caps the result window at offset+limit <= 1000 (paging past
# it returns empty), so only the first 1000 matches are reachable. We randomize
# across those pages; that's plenty of rotation for an e-ink frame.
_RESULT_WINDOW = 1000
_MAX_PAGE = _RESULT_WINDOW // _PAGE_SIZE
# How many random pages to try before giving up (each may be all null-image).
_MAX_TRIES = 4


class ArtInstituteMode(Mode):
    name = "art_institute"
    description = "A random public-domain artwork from the Art Institute of Chicago."
    periodic = True
    #: Continuous-tone artwork: the server dithers it to the exact panel palette,
    #: so the device just packs it nearest-color (no second on-panel dither).
    epd_mode = "fastest"

    async def generate(self, ctx: ModeContext) -> Optional[ContentItem]:
        cfg = ctx.settings
        try:
            base_params: dict[str, object] = {
                "query[term][is_public_domain]": "true",
                "fields": "id,title,image_id,artist_title,date_display",
                "limit": _PAGE_SIZE,
            }
            if cfg.aic_search_query:
                base_params["q"] = cfg.aic_search_query

            # 1) How many public-domain artworks match the (optional) query.
            head = (
                await ctx.http.get(
                    _SEARCH_URL, params=base_params, headers=_HEADERS, timeout=15
                )
            ).json()
            total = int(head.get("pagination", {}).get("total", 0))
            if total <= 0:
                raise ValueError("no public-domain artworks matched")
            pages = max(1, min(_MAX_PAGE, -(-total // _PAGE_SIZE)))

            # 2) Pull random pages until one yields a record with an image.
            art = None
            for _ in range(_MAX_TRIES):
                params = dict(base_params)
                params["page"] = random.randint(1, pages)
                data = (
                    await ctx.http.get(
                        _SEARCH_URL, params=params, headers=_HEADERS, timeout=15
                    )
                ).json().get("data") or []
                with_image = [a for a in data if a.get("image_id")]
                if with_image:
                    art = random.choice(with_image)
                    break
            if art is None:
                raise ValueError("no artwork with an image found")

            img_url = (
                f"{_IIIF_BASE}/{art['image_id']}"
                f"/full/{cfg.aic_iiif_width},/0/default.jpg"
            )
            img_resp = await ctx.http.get(img_url, headers=_HEADERS, timeout=20)
            img_resp.raise_for_status()

            return ContentItem(
                kind=ContentKind.image,
                title=art.get("title") or "Artwork",
                image_bytes=img_resp.content,
            )
        except Exception as exc:  # network or parsing failure
            logger.warning("art_institute generation failed: %s", exc)
            return ContentItem(
                kind=ContentKind.text,
                title="Art Institute",
                text="Could not fetch artwork right now.",
            )
