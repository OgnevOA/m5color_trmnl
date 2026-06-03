"""Fallback placeholder mode for unknown/unsupported mode names."""

from __future__ import annotations

from typing import Optional

from .base import ContentItem, ContentKind, Mode, ModeContext


class PlaceholderUnknownMode(Mode):
    name = "placeholder_unknown_mode"
    description = "Placeholder shown when the selected mode is unknown."

    def __init__(self, requested: str | None = None) -> None:
        self._requested = requested

    async def generate(self, ctx: ModeContext) -> Optional[ContentItem]:
        requested = self._requested or "unknown"
        return ContentItem(
            kind=ContentKind.text,
            title="Unknown Mode",
            text=f"Mode '{requested}' is not available yet.",
        )
