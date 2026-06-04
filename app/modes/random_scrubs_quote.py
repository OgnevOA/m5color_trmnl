"""Random quote mode backed by ``scrubs.json`` (Scrubs)."""

from __future__ import annotations

from .quote_mode import JsonQuoteMode


class RandomScrubsQuoteMode(JsonQuoteMode):
    name = "random_scrubs_quote"
    description = "Display a random quote from Scrubs."
    data_file = "scrubs.json"
    show_title = "Scrubs"
    logo_file = "scrubs_header.png"
    fallback_text = "Newbie..."
