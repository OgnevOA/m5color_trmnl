"""Random quote mode using a built-in collection of Friends quotes."""

from __future__ import annotations

import random
from typing import Optional

from .base import ContentItem, ContentKind, Mode, ModeContext

_QUOTES: list[tuple[str, str]] = [
    ("Joey", "How you doin'?"),
    ("Ross", "We were on a break!"),
    ("Chandler", "Could I BE wearing any more clothes?"),
    ("Monica", "Welcome to the real world. It sucks. You're gonna love it."),
    ("Phoebe", "Oh, I wish I could, but I don't want to."),
    ("Rachel", "It's like all my life everyone has told me, 'You're a shoe!'"),
    ("Joey", "Joey doesn't share food!"),
    ("Chandler", "Hi, I'm Chandler. I make jokes when I'm uncomfortable."),
    ("Ross", "Pivot! PIVOT! PIV-OT!"),
    ("Phoebe", "They don't know that we know they know we know."),
    ("Monica", "Rules help control the fun."),
    ("Janice", "Oh. My. God."),
]


class RandomFriendsQuoteMode(Mode):
    name = "random_friends_quote"
    description = "Display a random quote from Friends."

    async def generate(self, ctx: ModeContext) -> Optional[ContentItem]:
        speaker, quote = random.choice(_QUOTES)
        return ContentItem(
            kind=ContentKind.text,
            title=f"Friends - {speaker}",
            text=f"\u201c{quote}\u201d",
        )
