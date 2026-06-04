"""Background pre-render worker.

Polls the queue for pending items and renders each into a 400x600 Spectra-6
PNG stored on disk. This is the ONLY place Chromium runs -- never on the
device request path.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .. import queue_service
from ..config import Settings
from ..db import Database
from ..models import QueueItem, QueueItemKind
from ..modes.quote_mode import JsonQuoteMode
from ..modes.registry import get_mode
from . import image_ops
from .browser import BrowserRenderer
from .templates import render_text_html

logger = logging.getLogger(__name__)


def _is_quote_item(item: QueueItem) -> bool:
    """True if the item was produced by a quote mode (Friends/Office/Scrubs/...)."""
    if not item.mode_name:
        return False
    return isinstance(get_mode(item.mode_name), JsonQuoteMode)


class PreRenderWorker:
    """Async loop that renders queued content ahead of device wake-ups."""

    def __init__(
        self,
        db: Database,
        settings: Settings,
        renderer: BrowserRenderer,
        poll_interval: float = 2.0,
    ) -> None:
        self._db = db
        self._settings = settings
        self._renderer = renderer
        self._poll_interval = poll_interval
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        #: Set when the worker should immediately re-check the queue.
        self._wakeup = asyncio.Event()

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="prerender-worker")
            logger.info("Pre-render worker started")

    async def stop(self) -> None:
        self._stop.set()
        self._wakeup.set()
        if self._task is not None:
            await self._task
            self._task = None
        logger.info("Pre-render worker stopped")

    def notify(self) -> None:
        """Signal that new content was enqueued (renders sooner)."""
        self._wakeup.set()

    async def _run(self) -> None:
        device_id = self._settings.device_id
        while not self._stop.is_set():
            try:
                rendered_any = await self._drain_queue(device_id)
            except Exception:  # pragma: no cover - defensive
                logger.exception("Unexpected error in pre-render worker")
                rendered_any = False

            if rendered_any:
                continue  # keep draining while there is work

            # Wait for either a wakeup signal or the poll timeout.
            try:
                await asyncio.wait_for(
                    self._wakeup.wait(), timeout=self._poll_interval
                )
            except asyncio.TimeoutError:
                pass
            self._wakeup.clear()

    async def _drain_queue(self, device_id: str) -> bool:
        item = await queue_service.next_pending(self._db, device_id)
        if item is None:
            return False
        await self._render_item(item)
        return True

    async def _render_item(self, item: QueueItem) -> None:
        try:
            if item.kind == QueueItemKind.image:
                png = await self._render_image_item(item)
            else:
                png = await self._render_html_item(item)

            image_id = await queue_service.next_image_id(self._db)
            path = Path(self._settings.rendered_images_dir) / f"{image_id}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(png)
            await queue_service.record_rendered(
                self._db,
                device_id=item.device_id,
                queue_item_id=item.id,
                image_id=image_id,
                path=str(path),
                width=image_ops.TARGET_WIDTH,
                height=image_ops.TARGET_HEIGHT,
            )
            logger.info("Rendered queue item %s -> %s", item.id, image_id)
        except Exception as exc:
            logger.exception("Failed to render queue item %s", item.id)
            await queue_service.mark_failed(self._db, item.id, str(exc))

    async def _render_image_item(self, item: QueueItem) -> bytes:
        if not item.source_path or not Path(item.source_path).exists():
            raise FileNotFoundError(f"source image missing: {item.source_path}")
        data = Path(item.source_path).read_bytes()
        return image_ops.png_bytes_to_display_png(data, fit_mode="cover")

    async def _render_html_item(self, item: QueueItem) -> bytes:
        if item.kind == QueueItemKind.html and item.html_content:
            html = item.html_content
        else:
            html = render_text_html(
                body=item.text_content or "",
                title=item.title or "Message",
            )
        screenshot = await self._renderer.render_html(html)
        # Quote cards are pure black-on-white: dithering only adds speckle to
        # anti-aliased text edges, so render them with a hard threshold. Other
        # text (e.g. plain messages with emoji) keeps dithering for color.
        dither = not _is_quote_item(item)
        # Screenshot is already 400x600; cover keeps it and applies palette.
        return image_ops.png_bytes_to_display_png(
            screenshot, fit_mode="cover", dither=dither
        )
