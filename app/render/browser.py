"""Headless Chromium rendering via Playwright.

A single browser instance is launched for the lifetime of the process and
reused across renders. This module is only ever invoked by the background
pre-render worker -- never on the device request path.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from playwright.async_api import Browser, Playwright, async_playwright

from .image_ops import TARGET_HEIGHT, TARGET_WIDTH

logger = logging.getLogger(__name__)


class BrowserRenderer:
    """Renders HTML strings into 400x600 PNG screenshots."""

    def __init__(self) -> None:
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._browser is not None:
            return
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        logger.info("Chromium browser launched for rendering")

    async def stop(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        logger.info("Chromium browser stopped")

    async def render_html(self, html: str) -> bytes:
        """Render an HTML document and return a 400x600 PNG screenshot."""
        if self._browser is None:
            raise RuntimeError("BrowserRenderer.start() must be called first")

        # Serialize renders: a single page at a fixed viewport is plenty for
        # this workload and keeps memory usage low.
        async with self._lock:
            context = await self._browser.new_context(
                viewport={"width": TARGET_WIDTH, "height": TARGET_HEIGHT},
                device_scale_factor=1,
            )
            page = await context.new_page()
            try:
                await page.set_content(html, wait_until="networkidle")
                png = await page.screenshot(
                    clip={
                        "x": 0,
                        "y": 0,
                        "width": TARGET_WIDTH,
                        "height": TARGET_HEIGHT,
                    }
                )
            finally:
                await context.close()
            return png
