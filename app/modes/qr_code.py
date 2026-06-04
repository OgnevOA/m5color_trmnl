"""QR mode: render user-supplied text/URL as a QR code.

Triggered by sending a message prefixed with ``qr:`` (e.g. ``qr: HELLO`` or
``qr: https://example.com``). Like the other static modes it holds the display
until changed.
"""

from __future__ import annotations

from typing import Optional

from .base import ContentItem, ContentKind, Mode, ModeContext


class QrCodeMode(Mode):
    name = "qr"
    description = "Render text/URL sent with a 'qr:' prefix as a QR code."
    periodic = False

    async def generate(self, ctx: ModeContext) -> Optional[ContentItem]:
        # Nothing to auto-generate; content comes from "qr: ..." messages.
        return ContentItem(
            kind=ContentKind.text,
            title="QR Code",
            text="Send a message like 'qr: HELLO' or 'qr: https://example.com'.",
        )
