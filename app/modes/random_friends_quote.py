"""Random quote mode backed by a JSON collection of Friends quotes.

The quotes live in ``friends.json`` next to this module:

    {
      "quotes": [
        {
          "id": 1,
          "dialogue": [{"speaker": "Monica Geller", "text": "..."}],
          "season": 1, "episode": 1, "episode_title": "..."
        },
        ...
      ]
    }
"""

from __future__ import annotations

import json
import logging
import random
from functools import lru_cache
from pathlib import Path
from typing import Optional

from ..render.templates import render_friends_quote_html
from .base import ContentItem, ContentKind, Mode, ModeContext

logger = logging.getLogger(__name__)

_DATA_PATH = Path(__file__).parent / "friends.json"


@lru_cache(maxsize=1)
def _load_quotes() -> tuple[dict, ...]:
    """Load and cache the quote collection. Returns an empty tuple on error."""
    try:
        data = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
        quotes = data.get("quotes", [])
        # Keep only entries that actually have dialogue with text.
        valid = [
            q
            for q in quotes
            if isinstance(q.get("dialogue"), list)
            and any(d.get("text") for d in q["dialogue"])
        ]
        logger.info("loaded %d Friends quotes", len(valid))
        return tuple(valid)
    except Exception as exc:
        logger.warning("could not load %s: %s", _DATA_PATH.name, exc)
        return tuple()


def _attribution(quote: dict) -> str:
    season = quote.get("season")
    episode = quote.get("episode")
    title = quote.get("episode_title")
    parts = []
    if season and episode:
        parts.append(f"S{season} \u00b7 E{episode}")
    if title:
        parts.append(str(title))
    return " \u2014 ".join(parts)


class RandomFriendsQuoteMode(Mode):
    name = "random_friends_quote"
    description = "Display a random quote from Friends."

    async def generate(self, ctx: ModeContext) -> Optional[ContentItem]:
        quotes = _load_quotes()
        if not quotes:
            return ContentItem(
                kind=ContentKind.text,
                title="Friends",
                text="How you doin'?",
            )

        quote = random.choice(quotes)
        dialogue = [
            {"speaker": str(d.get("speaker", "")).strip(), "text": str(d.get("text", "")).strip()}
            for d in quote.get("dialogue", [])
            if d.get("text")
        ]
        html = render_friends_quote_html(dialogue, attribution=_attribution(quote))
        return ContentItem(kind=ContentKind.html, title="Friends", html=html)
