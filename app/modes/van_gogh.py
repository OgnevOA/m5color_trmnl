"""Van Gogh mode: a random Van Gogh painting, portrait-first.

Pulls works whose creator is Vincent van Gogh from Wikidata, then resolves the
image (and its dimensions, to prefer portrait orientation) via the Wikimedia
Commons API. Public-domain, no API key. Like the other periodic modes the
network I/O happens off the device wake path and it degrades to a text fallback
on any error.

Source pipeline (3 requests per refill):
  1. Wikidata SPARQL: all ?image where creator (P170) == the artist QID.
  2. Commons API: one batched imageinfo call for ~50 random files -> sizes +
     scaled thumbnail URLs. Portrait (height > width) candidates are preferred;
     landscape is only used if a batch happens to contain no portraits.
  3. Download the chosen thumbnail.

Two User-Agents are used on purpose: Wikimedia's API etiquette wants a
descriptive UA, but ``upload.wikimedia.org`` enforces a "robot policy" that
403s bot-looking UAs for the image bytes, so the actual image download uses a
browser UA (which it accepts).
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
import urllib.parse
from typing import Optional

from .base import ContentItem, ContentKind, Mode, ModeContext

logger = logging.getLogger(__name__)

_SPARQL_URL = "https://query.wikidata.org/sparql"
_COMMONS_API = "https://commons.wikimedia.org/w/api.php"
_FILEPATH_MARK = "Special:FilePath/"

#: Descriptive UA for the Wikidata/Commons *APIs* (their etiquette wants this).
_API_UA = "m5color_trmnl-eink/1.0 (https://github.com/oleg/m5color_trmnl)"
#: Browser UA for the image bytes; upload.wikimedia.org's robot policy 403s
#: bot-looking UAs but serves browsers.
_IMG_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

#: Wikidata QID for Vincent van Gogh (creator, P170).
_DEFAULT_ARTIST_QID = "Q5582"
#: How many random files to resolve per refill (Commons allows 50 titles/call).
_BATCH = 50
#: Requested thumbnail width; large enough for both the 400x600 M5 and the
#: 1200x1600 E1004 after downstream fit/crop.
_THUMB_WIDTH = 1200

#: An artist's catalogue barely changes, and WDQS is rate-limited and outage-
#: prone, so cache the SPARQL file list per QID and refresh it at most weekly
#: (stale results are served if a later refresh fails). Module-level because the
#: worker creates a fresh mode instance per refill.
_CACHE_TTL = 7 * 24 * 3600
_files_cache: dict[str, tuple[float, list[str]]] = {}
_cache_lock = asyncio.Lock()


class VanGoghMode(Mode):
    name = "van_gogh"
    description = "A random Van Gogh painting (portrait-first), via Wikidata/Commons."
    periodic = True
    #: Continuous-tone artwork: the server dithers it to the exact panel palette,
    #: so the device just packs it nearest-color (no second on-panel dither).
    epd_mode = "fastest"

    async def generate(self, ctx: ModeContext) -> Optional[ContentItem]:
        try:
            qid = self._artist_qid(ctx)
            files = await self._artist_image_files(ctx, qid)
            if not files:
                raise ValueError("no works with images for artist")

            random.shuffle(files)
            info = await self._resolve_batch(ctx, files[:_BATCH])
            if not info:
                raise ValueError("no resolvable images in batch")

            portraits = [i for i in info if i["height"] > i["width"]]
            chosen = random.choice(portraits or info)  # portrait-first

            img_resp = await ctx.http.get(
                chosen["thumburl"], headers={"User-Agent": _IMG_UA}, timeout=30
            )
            img_resp.raise_for_status()
            return ContentItem(
                kind=ContentKind.image,
                title=chosen["title"],
                image_bytes=img_resp.content,
            )
        except Exception as exc:  # network or parsing failure
            logger.warning("van_gogh generation failed: %s", exc)
            return ContentItem(
                kind=ContentKind.text,
                title="Van Gogh",
                text="Could not fetch artwork right now.",
            )

    def _artist_qid(self, ctx: ModeContext) -> str:
        qid = getattr(ctx.settings, "van_gogh_artist_qid", "") or _DEFAULT_ARTIST_QID
        # Guard the QID before splicing it into the SPARQL query.
        return qid if re.fullmatch(r"Q\d+", qid) else _DEFAULT_ARTIST_QID

    async def _artist_image_files(self, ctx: ModeContext, qid: str) -> list[str]:
        """Commons file names for the artist's works, cached per QID (weekly).

        On a cache miss it queries WDQS; if that fails but a stale list exists,
        the stale list is returned so a transient WDQS outage doesn't break the
        mode.
        """
        cached = _files_cache.get(qid)
        if cached and (time.time() - cached[0]) < _CACHE_TTL:
            return cached[1]

        async with _cache_lock:
            cached = _files_cache.get(qid)  # re-check after acquiring the lock
            if cached and (time.time() - cached[0]) < _CACHE_TTL:
                return cached[1]
            try:
                files = await self._fetch_image_files(ctx, qid)
            except Exception as exc:
                if cached:
                    logger.warning("WDQS refresh failed (%s); serving cached list", exc)
                    return cached[1]
                raise
            if files:
                _files_cache[qid] = (time.time(), files)
            elif cached:
                return cached[1]
            return files

    async def _fetch_image_files(self, ctx: ModeContext, qid: str) -> list[str]:
        query = f"SELECT ?image WHERE {{ ?item wdt:P170 wd:{qid} ; wdt:P18 ?image . }}"
        resp = await ctx.http.get(
            _SPARQL_URL,
            params={"query": query, "format": "json"},
            headers={"User-Agent": _API_UA, "Accept": "application/json"},
            timeout=40,
        )
        resp.raise_for_status()
        rows = resp.json().get("results", {}).get("bindings", [])
        files = []
        for row in rows:
            value = row.get("image", {}).get("value", "")
            if _FILEPATH_MARK in value:
                files.append(urllib.parse.unquote(value.split(_FILEPATH_MARK)[-1]))
        return files

    async def _resolve_batch(
        self, ctx: ModeContext, files: list[str]
    ) -> list[dict]:
        """One imageinfo call -> [{title,width,height,thumburl}, ...]."""
        titles = "|".join(f"File:{name}" for name in files)
        resp = await ctx.http.get(
            _COMMONS_API,
            params={
                "action": "query",
                "format": "json",
                "titles": titles,
                "prop": "imageinfo",
                "iiprop": "size|url",
                "iiurlwidth": _THUMB_WIDTH,
            },
            headers={"User-Agent": _API_UA, "Accept": "application/json"},
            timeout=40,
        )
        resp.raise_for_status()
        pages = resp.json().get("query", {}).get("pages", {})
        out = []
        for page in pages.values():
            info = page.get("imageinfo")
            if not info:
                continue
            entry = info[0]
            thumb = entry.get("thumburl")
            width = entry.get("width")
            height = entry.get("height")
            if not thumb or not width or not height:
                continue
            raw = (page.get("title") or "Van Gogh").removeprefix("File:")
            title = re.sub(r"\.\w+$", "", raw).replace("_", " ").strip() or "Van Gogh"
            out.append(
                {"title": title, "width": width, "height": height, "thumburl": thumb}
            )
        return out
