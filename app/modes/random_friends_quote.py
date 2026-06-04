"""Random quote mode backed by ``friends.json``."""

from __future__ import annotations

from .quote_mode import JsonQuoteMode


class RandomFriendsQuoteMode(JsonQuoteMode):
    name = "random_friends_quote"
    description = "Display a random quote from Friends."
    data_file = "friends.json"
    show_title = "Friends"
    fallback_text = "How you doin'?"
