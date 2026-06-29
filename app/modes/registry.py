"""Registry of available content modes.

Adding a future mode (weather, calendar, now_playing, ...) is just a matter of
implementing :class:`~app.modes.base.Mode` and registering it here.
"""

from __future__ import annotations

from .base import Mode
from .image import ImageMode
from .now_playing import NowPlayingMode
from .placeholder_unknown import PlaceholderUnknownMode
from .plain_text import PlainTextMode
from .qr_code import QrCodeMode
from .random_friends_quote import RandomFriendsQuoteMode
from .random_office_quote import RandomOfficeQuoteMode
from .random_scrubs_quote import RandomScrubsQuoteMode
from .random_xkcd import RandomXkcdMode
from .van_gogh import VanGoghMode
from .weather import WeatherMode

_MODE_CLASSES: dict[str, type[Mode]] = {
    PlainTextMode.name: PlainTextMode,
    ImageMode.name: ImageMode,
    QrCodeMode.name: QrCodeMode,
    WeatherMode.name: WeatherMode,
    NowPlayingMode.name: NowPlayingMode,
    RandomFriendsQuoteMode.name: RandomFriendsQuoteMode,
    RandomOfficeQuoteMode.name: RandomOfficeQuoteMode,
    RandomScrubsQuoteMode.name: RandomScrubsQuoteMode,
    RandomXkcdMode.name: RandomXkcdMode,
    VanGoghMode.name: VanGoghMode,
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
