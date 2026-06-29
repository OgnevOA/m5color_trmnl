"""Metropolitan Museum of Art mode: show a random public-domain artwork.

On each refill it picks a random public-domain, open-access artwork from the
Met's free Collection API and returns its image as an ``image`` content item.
Like ``random_xkcd`` the network I/O happens off the device wake path and it
degrades to a text fallback if a request fails. No API key required.

Why the Met and not the Art Institute of Chicago: AIC serves its IIIF images
from behind a Cloudflare managed challenge that 403s unattended server clients,
so it can't be fetched from the device server. The Met's image CDN
(``images.metmuseum.org``) has no such bot wall.
"""

from __future__ import annotations

import logging
import random
from typing import Optional

from .base import ContentItem, ContentKind, Mode, ModeContext

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://collectionapi.metmuseum.org/public/collection/v1/search"
_OBJECTS_URL = "https://collectionapi.metmuseum.org/public/collection/v1/objects"
_OBJECT_URL = "https://collectionapi.metmuseum.org/public/collection/v1/objects/{}"
_HEADERS = {"User-Agent": "m5color_trmnl e-ink frame (github.com/oleg/m5color_trmnl)"}
# How many random objects to probe for a usable public-domain image. The full
# object listing is unfiltered, where roughly half of objects are public-domain
# with an image, so a handful of tries finds one with very high probability.
_MAX_TRIES = 10


class MetMuseumMode(Mode):
    name = "met_museum"
    description = "A random public-domain artwork from the Metropolitan Museum of Art."
    periodic = True
    #: Continuous-tone artwork: the server dithers it to the exact panel palette,
    #: so the device just packs it nearest-color (no second on-panel dither).
    epd_mode = "fastest"

    async def generate(self, ctx: ModeContext) -> Optional[ContentItem]:
        cfg = ctx.settings
        try:
            object_ids = await self._candidate_ids(ctx)
            if not object_ids:
                raise ValueError("no candidate artworks returned")

            for _ in range(_MAX_TRIES):
                oid = random.choice(object_ids)
                obj = (
                    await ctx.http.get(
                        _OBJECT_URL.format(oid), headers=_HEADERS, timeout=15
                    )
                ).json()
                if not obj.get("isPublicDomain"):
                    continue
                img_url = obj.get("primaryImage") or obj.get("primaryImageSmall")
                if not img_url:
                    continue
                img_resp = await ctx.http.get(img_url, headers=_HEADERS, timeout=30)
                img_resp.raise_for_status()
                title = obj.get("title") or "Artwork"
                return ContentItem(
                    kind=ContentKind.image,
                    title=title,
                    image_bytes=img_resp.content,
                )
            raise ValueError("no public-domain artwork with an image found")
        except Exception as exc:  # network or parsing failure
            logger.warning("met_museum generation failed: %s", exc)
            return ContentItem(
                kind=ContentKind.text,
                title="Met Museum",
                text="Could not fetch artwork right now.",
            )

    async def _candidate_ids(self, ctx: ModeContext) -> list[int]:
        """Object IDs to sample from: curated search, else the whole collection.

        A configured query uses the search endpoint, which pre-filters to
        public-domain works that have images, yielding a tight candidate set.
        With no query we sample the full object-id listing (~500k objects) and
        rely on the per-object public-domain/image check in :meth:`generate`
        (the listing itself is unfiltered). The Met search has no real match-all
        token, so the listing -- not a wildcard query -- is the random source.
        """
        query = ctx.settings.met_search_query
        if query:
            params = {"q": query, "hasImages": "true", "isPublicDomain": "true"}
            resp = await ctx.http.get(
                _SEARCH_URL, params=params, headers=_HEADERS, timeout=15
            )
        else:
            resp = await ctx.http.get(_OBJECTS_URL, headers=_HEADERS, timeout=20)
        return resp.json().get("objectIDs") or []
