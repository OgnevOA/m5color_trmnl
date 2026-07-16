"""Content-aware info overlay for artwork/photo frames.

When the overlay is enabled the pre-render worker draws the artwork as a
full-bleed background and lays a compact info band across the bottom quarter of
the display: a mini month calendar on one side and the date, an artwork caption
and the weather on the other.

This module only *prepares* the data (fit the background, build the calendar,
sample per-region luminance to pick a legible theme, summarize the weather).
The actual HTML/screenshot render stays in the worker + Playwright pipeline via
:func:`app.render.templates.render_overlay_html`.
"""

from __future__ import annotations

import base64
import calendar
import io
import logging
from datetime import datetime
from typing import Optional

import numpy as np
from PIL import Image

from . import image_ops

logger = logging.getLogger(__name__)

#: Fraction of the display height the overlay band occupies (bottom strip).
OVERLAY_FRAC = 0.25
#: Fraction of the band width used by the calendar block (left); the caption +
#: weather block takes the rest. Kept in sync between layout and luminance
#: sampling so each block's theme matches what sits behind it.
CAL_FRAC = 0.46
#: Perceptual-luminance threshold (0-255): brighter regions get dark text.
_LUMA_THRESHOLD = 140.0

#: Sunday-first weekday headers (matches ``firstweekday=6`` below).
_WEEKHEAD = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


def fit_background(data: bytes, size: tuple[int, int]) -> Image.Image:
    """Decode raw image bytes and cover-fit them to ``size`` (RGB).

    Mirrors the plain image path (EXIF/orientation normalize + cover crop) so
    the overlaid frame is framed identically to the non-overlay one.
    """
    img = Image.open(io.BytesIO(data))
    img = image_ops.auto_orient(img)
    return image_ops.fit_to_size(img, size, mode="cover")


def image_to_data_uri(img: Image.Image) -> str:
    """Encode a PIL image as a base64 ``data:`` PNG URI for CSS backgrounds."""
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def build_calendar(now: datetime) -> dict:
    """Build a Sunday-first month matrix with today flagged."""
    cal = calendar.Calendar(firstweekday=6)  # 6 == Sunday
    weeks = [
        [{"day": day or None, "today": day == now.day} for day in week]
        for week in cal.monthdayscalendar(now.year, now.month)
    ]
    return {
        "month_label": now.strftime("%B %Y"),
        "weekhead": _WEEKHEAD,
        "weeks": weeks,
    }


def _region_theme(img: Image.Image, box: tuple[int, int, int, int]) -> str:
    """Return ``"light"`` (light text/dark scrim) or ``"dark"`` for a crop.

    A bright region needs dark text; a dark region needs light text.
    """
    region = img.crop(box)
    if region.width == 0 or region.height == 0:
        return "light"
    # Downsample first: the mean is all we need and 24x24 is plenty.
    small = region.convert("RGB").resize((24, 24), Image.BILINEAR)
    arr = np.asarray(small, dtype=np.float32)
    luma = 0.299 * arr[..., 0] + 0.587 * arr[..., 1] + 0.114 * arr[..., 2]
    return "dark" if float(luma.mean()) > _LUMA_THRESHOLD else "light"


def choose_block_themes(fitted: Image.Image) -> tuple[str, str]:
    """Pick a per-block theme from the luminance behind each block.

    Samples the calendar and caption/weather sub-regions of the bottom band
    separately, so a picture that is dark on one side and bright on the other
    stays legible on both. Returns ``(cal_theme, info_theme)``.
    """
    w, h = fitted.size
    top = int(h * (1.0 - OVERLAY_FRAC))
    split = int(w * CAL_FRAC)
    cal_theme = _region_theme(fitted, (0, top, split, h))
    info_theme = _region_theme(fitted, (split, top, w, h))
    return cal_theme, info_theme


async def get_weather_summary(http, settings) -> Optional[dict]:
    """Compact weather summary for the overlay, or ``None`` if unavailable."""
    from ..modes.weather import fetch_weather_display  # lazy: avoid import cycle

    data = await fetch_weather_display(http, settings)
    if not data:
        return None
    return {
        "temp": data.get("temp"),
        "unit": data.get("unit", ""),
        "condition": data.get("condition", ""),
    }


def build_context(
    now: datetime,
    caption_title: Optional[str],
    caption_artist: Optional[str],
    weather: Optional[dict],
    cal_theme: str,
    info_theme: str,
) -> dict:
    """Assemble the full template context for :func:`render_overlay_html`."""
    ctx = build_calendar(now)
    ctx.update(
        {
            "date_str": now.strftime("%A, %B %-d"),
            "caption_title": caption_title,
            "caption_artist": caption_artist,
            "weather": weather,
            "cal_theme": cal_theme,
            "info_theme": info_theme,
            "overlay_pct": round(OVERLAY_FRAC * 100),
            "cal_pct": round(CAL_FRAC * 100),
        }
    )
    return ctx
