"""Artist modes: a random public-domain painting by a chosen artist.

Each concrete mode (Van Gogh, Monet, Caravaggio, Klimt, ...) is the same
pipeline pointed at a different Wikidata artist QID, so users pick the painter
straight from the mode menu. Works whose creator is that artist are pulled from
Wikidata, then the image (and its dimensions, to prefer portrait orientation)
is resolved via the Wikimedia Commons API. Public-domain, no API key. Like the
other periodic modes the network I/O happens off the device wake path and it
degrades to a text fallback on any error.

Source pipeline (per refill):
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
import base64
import io
import logging
import random
import re
import time
import urllib.parse
from typing import Optional

from PIL import Image

from ..llm import clean_painting_title
from ..render.templates import render_collage_html
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

#: How many random files to resolve per refill (Commons allows 50 titles/call).
_BATCH = 50
#: Requested thumbnail width; large enough for both the 400x600 M5 and the
#: 1200x1600 E1004 after downstream fit/crop.
_THUMB_WIDTH = 1200

#: An artist's catalogue barely changes, and WDQS is rate-limited and outage-
#: prone, so cache the SPARQL file list per QID and refresh it at most weekly
#: (stale results are served if a later refresh fails). Module-level because the
#: worker creates a fresh mode instance per refill; keyed by QID so every artist
#: mode has its own cached list. The cached value is
#: ``(fetched_at, filenames, {normalized_filename: year})``.
_CACHE_TTL = 7 * 24 * 3600
_files_cache: dict[str, tuple[float, list[str], dict[str, int]]] = {}
_cache_lock = asyncio.Lock()


def _normalize_filename(name: str) -> str:
    """Commons normalizes underscores to spaces in page titles; match that."""
    return name.replace("_", " ")


def _parse_year(value: str) -> Optional[int]:
    """Extract a 3-4 digit (CE) year from a Wikidata time literal, if any."""
    match = re.match(r"\+?(\d{3,4})-", value or "")
    return int(match.group(1)) if match else None


#: Max long-side (px) for a collage tile image before base64-embedding. Kept
#: small so the composed HTML (several images inline) stays a sane size; tiles
#: are shown at a fraction of the panel anyway.
_COLLAGE_TILE_PX = 700


def _tile_data_uri(data: bytes, max_px: int = _COLLAGE_TILE_PX) -> tuple[str, int, int]:
    """Downscale image bytes and return ``(jpeg_data_uri, width, height)``.

    JPEG (not PNG) keeps the embedded data URI small for continuous-tone
    paintings; the dimensions let the mosaic place the work by orientation.
    """
    img = Image.open(io.BytesIO(data)).convert("RGB")
    img.thumbnail((max_px, max_px), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=82, optimize=True)
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}", img.width, img.height


class ArtistMode(Mode):
    """Base mode: a random painting by ``artist_qid``, portrait-first.

    Concrete artist modes just set ``name``, ``description``, ``artist_qid``
    (Wikidata QID) and ``artist_label`` (shown as the title / text fallback).
    """

    periodic = True
    #: Continuous-tone artwork: the server dithers it to the exact panel palette,
    #: so the device just packs it nearest-color (no second on-panel dither).
    epd_mode = "fastest"

    #: Overridden per concrete artist mode.
    artist_qid: str = "Q5582"
    artist_label: str = "Artwork"

    #: Collage modifier: supported works-per-collage presets (each maps to a
    #: dedicated mosaic layout) and the default.
    COLLAGE_COUNTS: tuple[int, ...] = (4, 6, 9)
    COLLAGE_COUNT_DEFAULT: int = 6

    @classmethod
    def snap_collage_count(cls, count: int) -> int:
        """Clamp ``count`` to [3, 9] and snap it to the nearest supported preset."""
        try:
            value = int(count)
        except (TypeError, ValueError):
            return cls.COLLAGE_COUNT_DEFAULT
        value = max(3, min(9, value))
        return min(cls.COLLAGE_COUNTS, key=lambda c: (abs(c - value), c))

    async def generate(self, ctx: ModeContext) -> Optional[ContentItem]:
        try:
            files, years = await self._artist_image_files(ctx, self.artist_qid)
            if not files:
                raise ValueError("no works with images for artist")

            random.shuffle(files)
            info = await self._resolve_batch(ctx, files[:_BATCH], years)
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
                title=await self._compose_title(ctx, chosen),
                image_bytes=img_resp.content,
            )
        except Exception as exc:  # network or parsing failure
            logger.warning("%s generation failed: %s", self.name, exc)
            return ContentItem(
                kind=ContentKind.text,
                title=self.artist_label,
                text="Could not fetch artwork right now.",
            )

    async def generate_collage(
        self, ctx: ModeContext, count: int
    ) -> Optional[ContentItem]:
        """Compose ``count`` works by this artist into one mosaic (HTML frame).

        Unlike :meth:`generate`, this uses BOTH orientations (tiles are
        cover-cropped, so the full catalogue gives more variety) and returns an
        ``html`` item; the pre-render worker screenshots and dithers it. Any
        failure degrades to the same text fallback as :meth:`generate`.
        """
        try:
            n = self.snap_collage_count(count)
            files, years = await self._artist_image_files(ctx, self.artist_qid)
            if not files:
                raise ValueError("no works with images for artist")

            random.shuffle(files)
            info = await self._resolve_batch(ctx, files[:_BATCH], years)
            if len(info) < 3:
                raise ValueError("not enough resolvable images for a collage")

            random.shuffle(info)
            # A few extra candidates so a couple of failed downloads don't
            # shrink the mosaic below the requested count.
            candidates = info[: n + 4]
            results = await asyncio.gather(
                *(self._fetch_tile(ctx, c) for c in candidates),
                return_exceptions=True,
            )
            tiles = [t for t in results if isinstance(t, dict)][:n]
            if len(tiles) < 3:
                raise ValueError("could not download enough works for a collage")

            html = render_collage_html(self.artist_label, tiles)
            return ContentItem(
                kind=ContentKind.html, title=self.artist_label, html=html
            )
        except Exception as exc:
            logger.warning("%s collage generation failed: %s", self.name, exc)
            return ContentItem(
                kind=ContentKind.text,
                title=self.artist_label,
                text="Could not fetch artwork right now.",
            )

    async def _fetch_tile(self, ctx: ModeContext, chosen: dict) -> dict:
        """Download one work and build its collage tile (data URI + metadata)."""
        img_resp = await ctx.http.get(
            chosen["thumburl"], headers={"User-Agent": _IMG_UA}, timeout=30
        )
        img_resp.raise_for_status()
        uri, width, height = _tile_data_uri(img_resp.content)
        return {
            "uri": uri,
            "title": chosen.get("name"),
            "year": chosen.get("year"),
            "width": width,
            "height": height,
        }

    async def _artist_image_files(
        self, ctx: ModeContext, qid: str
    ) -> tuple[list[str], dict[str, int]]:
        """Commons file names + a ``{filename: year}`` map, cached per QID (weekly).

        On a cache miss it queries WDQS; if that fails but a stale list exists,
        the stale list is returned so a transient WDQS outage doesn't break the
        mode.
        """
        cached = _files_cache.get(qid)
        if cached and (time.time() - cached[0]) < _CACHE_TTL:
            return cached[1], cached[2]

        async with _cache_lock:
            cached = _files_cache.get(qid)  # re-check after acquiring the lock
            if cached and (time.time() - cached[0]) < _CACHE_TTL:
                return cached[1], cached[2]
            try:
                files, years = await self._fetch_image_files(ctx, qid)
            except Exception as exc:
                if cached:
                    logger.warning("WDQS refresh failed (%s); serving cached list", exc)
                    return cached[1], cached[2]
                raise
            if files:
                _files_cache[qid] = (time.time(), files, years)
            elif cached:
                return cached[1], cached[2]
            return files, years

    async def _fetch_image_files(
        self, ctx: ModeContext, qid: str
    ) -> tuple[list[str], dict[str, int]]:
        query = (
            f"SELECT ?image ?inception WHERE {{ "
            f"?item wdt:P170 wd:{qid} ; wdt:P18 ?image . "
            f"OPTIONAL {{ ?item wdt:P571 ?inception . }} }}"
        )
        resp = await ctx.http.get(
            _SPARQL_URL,
            params={"query": query, "format": "json"},
            headers={"User-Agent": _API_UA, "Accept": "application/json"},
            timeout=40,
        )
        resp.raise_for_status()
        rows = resp.json().get("results", {}).get("bindings", [])
        files: list[str] = []
        seen: set[str] = set()
        years: dict[str, int] = {}
        for row in rows:
            value = row.get("image", {}).get("value", "")
            if _FILEPATH_MARK not in value:
                continue
            fname = urllib.parse.unquote(value.split(_FILEPATH_MARK)[-1])
            if fname not in seen:
                seen.add(fname)
                files.append(fname)
            year = _parse_year(row.get("inception", {}).get("value", ""))
            if year is not None:
                years.setdefault(_normalize_filename(fname), year)
        return files, years

    async def _compose_title(self, ctx: ModeContext, chosen: dict) -> str:
        """Build the caption title: LLM-cleaned name + our reliable year.

        The year comes from Wikidata (trustworthy), so we clean only the messy
        filename-derived name via the LLM and append the year ourselves. Any LLM
        failure falls back to the regex-cleaned name.
        """
        name = chosen["name"]
        cleaned = await clean_painting_title(
            ctx.http, ctx.settings, name, self.artist_label
        )
        display = cleaned or name
        year = chosen.get("year")
        return f"{display} ({year})" if year else display

    async def _resolve_batch(
        self, ctx: ModeContext, files: list[str], years: dict[str, int]
    ) -> list[dict]:
        """One imageinfo call -> [{name,year,width,height,thumburl}, ...]."""
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
            raw = (page.get("title") or self.artist_label).removeprefix("File:")
            name = (
                re.sub(r"\.\w+$", "", raw).replace("_", " ").strip()
                or self.artist_label
            )
            year = years.get(_normalize_filename(raw))
            out.append(
                {
                    "name": name,
                    "year": year,
                    "width": width,
                    "height": height,
                    "thumburl": thumb,
                }
            )
        return out


class VanGoghMode(ArtistMode):
    name = "van_gogh"
    description = "A random Vincent van Gogh painting (portrait-first)."
    artist_qid = "Q5582"
    artist_label = "Van Gogh"


class MonetMode(ArtistMode):
    name = "monet"
    description = "A random Claude Monet painting (portrait-first)."
    artist_qid = "Q296"
    artist_label = "Claude Monet"


class CaravaggioMode(ArtistMode):
    name = "caravaggio"
    description = "A random Caravaggio painting (portrait-first)."
    artist_qid = "Q42207"
    artist_label = "Caravaggio"


class KlimtMode(ArtistMode):
    name = "klimt"
    description = "A random Gustav Klimt painting (portrait-first)."
    artist_qid = "Q34661"
    artist_label = "Gustav Klimt"
