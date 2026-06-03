"""Registry of available content modes.

Adding a future mode (weather, calendar, now_playing, ...) is just a matter of
implementing :class:`~app.modes.base.Mode` and registering it here.
"""

from __future__ import annotations

from .base import Mode
from .image import ImageMode
from .placeholder_unknown import PlaceholderUnknownMode
from .plain_text import PlainTextMode
from .random_friends_quote import RandomFriendsQuoteMode
from .random_xkcd import RandomXkcdMode

_MODE_CLASSES: dict[str, type[Mode]] = {
    PlainTextMode.name: PlainTextMode,
    ImageMode.name: ImageMode,
    RandomFriendsQuoteMode.name: RandomFriendsQuoteMode,
    RandomXkcdMode.name: RandomXkcdMode,
}

DEFAULT_MODE = PlainTextMode.name


def available_modes() -> list[str]:
    """Names of all registered (selectable) modes."""
    return list(_MODE_CLASSES.keys())


def is_known_mode(name: str) -> bool:
    return name in _MODE_CLASSES


def get_mode(name: str) -> Mode:
    """Return a mode instance, falling back to the placeholder mode."""
    mode_cls = _MODE_CLASSES.get(name)
    if mode_cls is None:
        return PlaceholderUnknownMode(requested=name)
    return mode_cls()
