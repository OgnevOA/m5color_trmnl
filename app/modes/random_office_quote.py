"""Random quote mode backed by ``office.json`` (The Office)."""

from __future__ import annotations

from .quote_mode import JsonQuoteMode


class RandomOfficeQuoteMode(JsonQuoteMode):
    name = "random_office_quote"
    description = "Display a random quote from The Office."
    data_file = "office.json"
    show_title = "The Office"
    fallback_text = "That's what she said."
