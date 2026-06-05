"""Home Assistant presence lookup for the display presence gate.

Used by the status decision path to decide whether anyone is home. Everything
here fails open: any missing config, timeout, or HTTP error yields ``None``
("unknown"), and the caller treats unknown as "do not gate" so HA downtime can
never freeze the display.
"""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import urlparse

import httpx

from .config import Settings

logger = logging.getLogger(__name__)

#: Keep the device wake path fast: HA is on the LAN, so this is plenty.
_TIMEOUT_SECONDS = 3.0

#: media_player states that mean something is on-screen and worth a poster.
_ACTIVE_STATES = {"playing", "paused", "buffering", "on"}

#: A little longer than presence: we are downloading the actual artwork.
_POSTER_TIMEOUT_SECONDS = 20.0


async def anyone_home(http: httpx.AsyncClient, settings: Settings) -> Optional[bool]:
    """Return whether at least one presence entity is home.

    ``True``  -> at least one entity reports ``state == "home"``.
    ``False`` -> all entities were reachable and none are home.
    ``None``  -> unconfigured / unreachable / error (caller does not gate).
    """
    if not settings.presence_gating_configured:
        return None

    base = settings.home_assistant_url.rstrip("/")
    headers = {"Authorization": f"Bearer {settings.home_assistant_token}"}

    saw_any = False
    for entity_id in settings.presence_entities:
        try:
            resp = await http.get(
                f"{base}/api/states/{entity_id}",
                headers=headers,
                timeout=_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            state = str(resp.json().get("state", "")).lower()
        except Exception as exc:
            # Fail open: if we cannot read HA reliably, do not gate the display.
            logger.warning("presence check failed for %s: %s", entity_id, exc)
            return None
        saw_any = True
        if state == "home":
            return True

    return False if saw_any else None


def _poster_signature(attrs: dict) -> str:
    """Stable identity for the currently-playing media (for dedupe).

    Prefers a content id; falls back to a join of human-readable fields, then
    to the artwork path (sans cache-busting query) as a last resort.
    """
    cid = attrs.get("media_content_id")
    if cid:
        return str(cid)
    parts = [
        attrs.get("media_title"),
        attrs.get("media_series_title"),
        attrs.get("media_season"),
        attrs.get("media_episode"),
        attrs.get("media_artist"),
        attrs.get("media_album_name"),
    ]
    sig = "|".join(str(p) for p in parts if p)
    if sig:
        return sig
    return urlparse(str(attrs.get("entity_picture", ""))).path


async def media_player_poster(
    http: httpx.AsyncClient, settings: Settings
) -> Optional[tuple[bytes, str]]:
    """Fetch the artwork of the configured media_player.

    Returns ``(image_bytes, signature)`` when something is playing and has
    artwork, otherwise ``None``. Fails open (returns ``None``) on any missing
    config, unreachable HA, idle player, or HTTP error.
    """
    if not settings.now_playing_configured:
        return None

    base = settings.home_assistant_url.rstrip("/")
    headers = {"Authorization": f"Bearer {settings.home_assistant_token}"}
    entity_id = settings.home_assistant_media_player_entity

    try:
        resp = await http.get(
            f"{base}/api/states/{entity_id}",
            headers=headers,
            timeout=_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        logger.warning("now playing: state fetch failed for %s: %s", entity_id, exc)
        return None

    state = str(payload.get("state", "")).lower()
    attrs = payload.get("attributes", {}) or {}
    picture = attrs.get("entity_picture")
    if state not in _ACTIVE_STATES or not picture:
        return None

    # Build the artwork URL and decide whether to send the HA token. Only attach
    # the bearer header when the artwork lives on the HA host; absolute external
    # URLs (e.g. TMDB) must never receive our token.
    picture = str(picture)
    if picture.startswith("http://") or picture.startswith("https://"):
        art_url = picture
        same_host = urlparse(picture).netloc == urlparse(base).netloc
    else:
        art_url = f"{base}/{picture.lstrip('/')}"
        same_host = True
    art_headers = headers if same_host else None

    try:
        img = await http.get(
            art_url, headers=art_headers, timeout=_POSTER_TIMEOUT_SECONDS
        )
        img.raise_for_status()
    except Exception as exc:
        logger.warning("now playing: artwork fetch failed: %s", exc)
        return None

    return img.content, _poster_signature(attrs)
