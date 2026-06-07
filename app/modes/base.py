"""Mode abstraction.

A *mode* knows how to generate a piece of content that will be turned into a
render job. Generation may perform network I/O (e.g. fetching an XKCD comic);
this happens off the device request path, when content is enqueued.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Optional

import httpx

if TYPE_CHECKING:
    from ..config import Settings


class ContentKind(str, Enum):
    text = "text"
    html = "html"
    image = "image"


@dataclass
class ContentItem:
    """A unit of content produced by a mode (or user input)."""

    kind: ContentKind
    title: str = "Message"
    text: Optional[str] = None
    html: Optional[str] = None
    image_bytes: Optional[bytes] = None


@dataclass
class ModeContext:
    """Resources passed to a mode during content generation."""

    http: httpx.AsyncClient
    settings: "Settings"


class Mode(abc.ABC):
    """Base class for all content modes."""

    name: str = "base"
    description: str = ""
    #: Mode category:
    #:   * periodic=True  -> the mode auto-generates fresh content (a new quote
    #:     or comic) on each wake (e.g. random_friends_quote, random_xkcd).
    #:   * periodic=False -> "static" mode: it shows user-supplied content and
    #:     holds the display until changed manually (plain_text, image).
    periodic: bool = True
    #: Device-side dithering hint sent on a ``draw``. The panel always does the
    #: single RGB->Spectra-6 conversion itself (the server now sends smooth RGB),
    #: so this only selects *how*:
    #:   * "fastest" -> nearest-color, no dithering. Best for flat/text content
    #:     (quotes, plain text, QR, line art, weather cards): crisp edges, no
    #:     speckle. This is the default since most modes are flat cards.
    #:   * "quality" -> ordered dithering for continuous-tone images (photos,
    #:     album art) so gradients don't band.
    epd_mode: str = "fastest"

    @abc.abstractmethod
    async def generate(self, ctx: ModeContext) -> Optional[ContentItem]:
        """Produce the next content item, or ``None`` if nothing to add."""
        raise NotImplementedError
