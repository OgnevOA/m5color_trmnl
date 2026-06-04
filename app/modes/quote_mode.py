"""Shared base for modes that show a random quote from a JSON collection.

Each collection lives in a JSON file next to this module with the shape::

    {
      "quotes": [
        {
          "id": 1,
          "dialogue": [{"speaker": "Name", "text": "..."}],
          "season": 1, "episode": 1, "episode_title": "..."
        },
        ...
      ]
    }

``season``, ``episode`` and ``episode_title`` are all optional.
"""

from __future__ import annotations

import json
import logging
import random
from functools import lru_cache
from pathlib import Path
from typing import Optional

from ..render.templates import render_quote_card_html
from .base import ContentItem, ContentKind, Mode, ModeContext

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent


@lru_cache(maxsize=None)
def _load_quotes(filename: str) -> tuple[dict, ...]:
    """Load and cache a quote collection. Returns an empty tuple on error."""
    path = _DATA_DIR / filename
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        quotes = data.get("quotes", [])
        valid = [
            q
            for q in quotes
            if isinstance(q.get("dialogue"), list)
            and any(d.get("text") for d in q["dialogue"])
        ]
        logger.info("loaded %d quotes from %s", len(valid), filename)
        return tuple(valid)
    except Exception as exc:
        logger.warning("could not load %s: %s", filename, exc)
        return tuple()


class JsonQuoteMode(Mode):
    """Base mode: pick a random quote from ``data_file`` and render a card."""

    #: Filename of the JSON collection (relative to the modes package).
    data_file: str = ""
    #: Title shown on the card (footer).
    show_title: str = ""
    #: Shown when the collection is empty/unreadable.
    fallback_text: str = "No quotes available."

    async def generate(self, ctx: ModeContext) -> Optional[ContentItem]:
        quotes = _load_quotes(self.data_file)
        if not quotes:
            return ContentItem(
                kind=ContentKind.text,
                title=self.show_title,
                text=self.fallback_text,
            )

        quote = random.choice(quotes)
        dialogue = [
            {
                "speaker": str(d.get("speaker", "")).strip(),
                "text": str(d.get("text", "")).strip(),
            }
            for d in quote.get("dialogue", [])
            if d.get("text")
        ]
        html = render_quote_card_html(
            self.show_title,
            dialogue,
            season=quote.get("season"),
            episode=quote.get("episode"),
            episode_title=quote.get("episode_title"),
        )
        return ContentItem(kind=ContentKind.html, title=self.show_title, html=html)
