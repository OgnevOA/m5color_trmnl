"""Mapping from OpenWeatherMap icon codes to normalized weather icons.

The normalized PNGs (palette-exact, flattened on white) are produced offline by
``scripts/normalize_weather_icons.py``. Here we map each OWM icon code to a file
and a semantic accent color drawn from the Spectra-6 palette.
"""

from __future__ import annotations

import base64
from functools import lru_cache
from pathlib import Path

_ICONS_DIR = Path(__file__).parent.parent / "assets" / "weather_icons" / "normalized"

# Spectra-6 accents (hex) used elsewhere on the card.
YELLOW = "#f6da48"
BLUE = "#426ed6"
BLACK = "#000000"

# OWM icon code (e.g. "01d") -> normalized filename.
ICON_BY_CODE: dict[str, str] = {
    "01d": "day_clear.png",
    "01n": "night_clear.png",
    "02d": "day_partial_cloud.png",
    "02n": "night_partial_cloud.png",
    "03d": "cloudy.png",
    "03n": "cloudy.png",
    "04d": "overcast.png",
    "04n": "overcast.png",
    "09d": "rain.png",
    "09n": "rain.png",
    "10d": "day_rain.png",
    "10n": "night_rain.png",
    "11d": "day_rain_thunder.png",
    "11n": "night_rain_thunder.png",
    "13d": "day_snow.png",
    "13n": "night_snow.png",
    "50d": "mist.png",
    "50n": "mist.png",
}

# Accent color per OWM condition group (first two digits of the icon code).
_ACCENT_BY_GROUP: dict[str, str] = {
    "01": YELLOW,
    "02": YELLOW,
    "03": BLACK,
    "04": BLACK,
    "09": BLUE,
    "10": BLUE,
    "11": BLUE,
    "13": BLUE,
    "50": BLACK,
}

_FALLBACK_ICON = "cloudy.png"


def accent_for(code: str) -> str:
    """Return the Spectra-6 accent hex for an OWM icon ``code``."""
    return _ACCENT_BY_GROUP.get((code or "")[:2], BLACK)


@lru_cache(maxsize=None)
def icon_data_uri(code: str) -> tuple[str, int, int] | None:
    """Return ``(data_uri, width, height)`` for an OWM icon code, or ``None``.

    Dimensions are read straight from the PNG IHDR so the template can place the
    icon at its natural pixel size (crisp, no browser scaling).
    """
    filename = ICON_BY_CODE.get(code, _FALLBACK_ICON)
    path = _ICONS_DIR / filename
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:image/png;base64,{encoded}", width, height
