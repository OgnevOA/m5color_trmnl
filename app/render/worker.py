"""Background pre-render worker.

Polls the queue for pending items and renders each into a 400x600 Spectra-6
PNG stored on disk. This is the ONLY place Chromium runs -- never on the
device request path.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import httpx

from .. import queue_service
from ..config import Settings
from ..db import Database
from ..models import QueueItem, QueueItemKind
from ..modes.artist import ArtistMode
from ..modes.registry import get_mode
from . import e1004, image_ops, overlay
from .browser import BrowserRenderer
from .templates import render_overlay_html, render_text_html

logger = logging.getLogger(__name__)


class PreRenderWorker:
    """Async loop that renders queued content ahead of device wake-ups."""

    def __init__(
        self,
        db: Database,
        settings: Settings,
        renderer: BrowserRenderer,
        http: Optional[httpx.AsyncClient] = None,
        poll_interval: float = 2.0,
    ) -> None:
        self._db = db
        self._settings = settings
        self._renderer = renderer
        self._http = http
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

    @property
    def _is_e1004(self) -> bool:
        return self._settings.device_type == "e1004"

    def _output_spec(self) -> tuple[int, int, str]:
        """Return ``(width, height, extension)`` for this device's frames."""
        if self._is_e1004:
            return e1004.E1004_WIDTH, e1004.E1004_HEIGHT, ".bin"
        return image_ops.TARGET_WIDTH, image_ops.TARGET_HEIGHT, ".png"

    def _device_size(self) -> tuple[int, int]:
        width, height, _ = self._output_spec()
        return width, height

    async def _overlay_enabled(self) -> bool:
        row = await self._db.fetchone(
            "SELECT overlay_enabled FROM settings WHERE device_id = ?",
            (self._settings.device_id,),
        )
        return bool(row["overlay_enabled"]) if row and row["overlay_enabled"] else False

    async def _render_item(self, item: QueueItem) -> None:
        try:
            t0 = time.monotonic()
            if item.kind == QueueItemKind.image:
                payload = await self._render_image_item(item)
            else:
                payload = await self._render_html_item(item)

            width, height, ext = self._output_spec()
            image_id = await queue_service.next_image_id(self._db)
            path = Path(self._settings.rendered_images_dir) / f"{image_id}{ext}"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            render_ms = int((time.monotonic() - t0) * 1000)
            await queue_service.record_rendered(
                self._db,
                device_id=item.device_id,
                queue_item_id=item.id,
                image_id=image_id,
                path=str(path),
                width=width,
                height=height,
                render_ms=render_ms,
            )
            logger.info("Rendered queue item %s -> %s", item.id, image_id)
            try:
                await queue_service.prune_rendered_images(
                    self._db,
                    item.device_id,
                    keep=self._settings.keep_rendered_images,
                )
                await queue_service.prune_source_files(self._db, item.device_id)
            except Exception:
                logger.exception("prune after render failed for %s", item.device_id)
        except Exception as exc:
            logger.exception("Failed to render queue item %s", item.id)
            await queue_service.mark_failed(self._db, item.id, str(exc))

    async def _render_image_item(self, item: QueueItem) -> bytes:
        if not item.source_path or not Path(item.source_path).exists():
            raise FileNotFoundError(f"source image missing: {item.source_path}")
        data = Path(item.source_path).read_bytes()

        # Optional content-aware info overlay (date/calendar/caption/weather).
        if await self._overlay_enabled():
            try:
                return await self._render_image_with_overlay(item, data)
            except Exception:
                # Never fail a frame over the overlay: fall back to the plain
                # image so the device always has something to draw.
                logger.exception(
                    "overlay render failed for item %s; using plain image", item.id
                )

        return self._render_plain_image(data)

    def _render_plain_image(self, data: bytes) -> bytes:
        if self._is_e1004:
            # E1004: pack directly into the driver's 4bpp GxEPD2 frame buffer.
            return e1004.render_e1004_frame(data)
        # Photos are continuous-tone: dither server-side (gamma-aware FS) to the
        # device's exact native palette so the panel draws it 1:1 with
        # epd_fastest (no second, coarser on-panel dither).
        return image_ops.dither_to_device_png(data, fit_mode="cover")

    def _caption_for(self, item: QueueItem) -> tuple[Optional[str], Optional[str]]:
        """Caption ``(title, artist)`` for the overlay, only for artist modes.

        Other image sources (user photos, generic image mode) get no caption --
        the overlay still shows the date, calendar and weather.
        """
        if not item.mode_name:
            return None, None
        mode = get_mode(item.mode_name)
        if isinstance(mode, ArtistMode):
            return item.title, mode.artist_label
        return None, None

    async def _render_image_with_overlay(self, item: QueueItem, data: bytes) -> bytes:
        size = self._device_size()
        fitted = overlay.fit_background(data, size)
        bg_uri = overlay.image_to_data_uri(fitted)
        cal_theme, info_theme = overlay.choose_block_themes(fitted)

        weather = None
        if self._http is not None:
            weather = await overlay.get_weather_summary(self._http, self._settings)

        title, artist = self._caption_for(item)
        now = datetime.now(ZoneInfo(self._settings.timezone))
        context = overlay.build_context(
            now, title, artist, weather, cal_theme, info_theme
        )
        html = render_overlay_html(bg_uri=bg_uri, **context)
        screenshot = await self._renderer.render_html(
            html, width=size[0], height=size[1]
        )
        if self._is_e1004:
            return e1004.render_e1004_frame(screenshot)
        # The composite is mostly continuous-tone artwork: FS-dither it to the
        # exact panel palette (the outlined overlay text stays crisp).
        return image_ops.dither_to_device_png(screenshot, fit_mode="cover")

    async def _render_html_item(self, item: QueueItem) -> bytes:
        if item.kind == QueueItemKind.html and item.html_content:
            html = item.html_content
        else:
            html = render_text_html(
                body=item.text_content or "",
                title=item.title or "Message",
            )
        if self._is_e1004:
            screenshot = await self._renderer.render_html(
                html, width=e1004.E1004_WIDTH, height=e1004.E1004_HEIGHT
            )
            return e1004.render_e1004_frame(screenshot)
        screenshot = await self._renderer.render_html(html)
        # Send the screenshot as smooth RGB; the device maps it to the panel
        # palette. Flat cards (quotes/QR/weather) use the "fastest" nearest-color
        # mode on-device, so anti-aliased text edges snap cleanly with no speckle.
        return image_ops.png_bytes_to_display_png(screenshot, fit_mode="cover")
