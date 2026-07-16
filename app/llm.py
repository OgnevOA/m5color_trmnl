"""Small LLM helpers (DeepSeek, OpenAI-compatible chat completions).

Currently just cleans the messy Wikimedia Commons filename-derived painting
titles into proper human titles for the artwork overlay caption. All calls are
best-effort: on a missing key, a bad response, or any network error the caller
falls back to the regex-cleaned filename, so the LLM is a pure enhancement and
never a dependency for a frame to render.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Optional

import httpx

if TYPE_CHECKING:
    from .config import Settings

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You clean up museum artwork titles. You are given a raw, "
    "filename-derived title of a painting and its artist. Respond with ONLY the "
    "proper title of the artwork in English, with: no artist name, no dates or "
    "years, no file extension, no catalogue/inventory numbers (e.g. F0000), no "
    "source or archive tags (e.g. 'Google Art Project', 'Wikimedia'), and no "
    "surrounding quotes. If you cannot determine a proper title, return a tidied "
    "version of the input. Reply with the title text only, on a single line."
)

#: Cache cleaned titles for the process lifetime, keyed by the raw title. The
#: LLM is deterministic enough here and titles repeat across refills, so this
#: keeps cost and latency down.
_cache: dict[str, str] = {}
_cache_lock = asyncio.Lock()

#: Sanity cap so a runaway response never becomes the caption.
_MAX_TITLE_LEN = 120


def _sanitize(text: str) -> str:
    """Trim a model reply to a single clean title line."""
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line.strip().strip("\"'\u201c\u201d\u2018\u2019").strip()


async def clean_painting_title(
    http: httpx.AsyncClient,
    settings: "Settings",
    raw_title: str,
    artist: str,
) -> Optional[str]:
    """Return an LLM-cleaned painting title, or ``None`` to use the fallback.

    ``None`` is returned when DeepSeek is not configured or the request fails /
    yields something unusable, so callers keep their regex-cleaned title.
    """
    raw_title = (raw_title or "").strip()
    if not settings.deepseek_api_key or not raw_title:
        return None

    cached = _cache.get(raw_title)
    if cached is not None:
        return cached or None  # cached "" means "LLM gave nothing usable"

    async with _cache_lock:
        cached = _cache.get(raw_title)
        if cached is not None:
            return cached or None
        try:
            resp = await http.post(
                f"{settings.deepseek_base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.deepseek_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": settings.deepseek_model,
                    "messages": [
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": f"Artist: {artist}\nRaw title: {raw_title}",
                        },
                    ],
                    "temperature": 0.0,
                    "max_tokens": 40,
                    "stream": False,
                },
                timeout=20,
            )
            resp.raise_for_status()
            content = (
                resp.json()["choices"][0]["message"]["content"]
            )
            cleaned = _sanitize(content)
            if not cleaned or len(cleaned) > _MAX_TITLE_LEN:
                cleaned = ""  # unusable -> cache the miss, fall back
        except Exception as exc:
            logger.warning("DeepSeek title cleanup failed: %s", exc)
            return None  # transient: don't cache, let a later refill retry

        _cache[raw_title] = cleaned
        return cleaned or None
