"""Mode abstraction.

A *mode* knows how to generate a piece of content that will be turned into a
render job. Generation may perform network I/O (e.g. fetching an XKCD comic);
this happens off the device request path, when content is enqueued.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import httpx


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

    @abc.abstractmethod
    async def generate(self, ctx: ModeContext) -> Optional[ContentItem]:
        """Produce the next content item, or ``None`` if nothing to add."""
        raise NotImplementedError
