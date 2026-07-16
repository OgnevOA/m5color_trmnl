"""Weather-for-today mode backed by OpenWeatherMap.

On each refill it fetches current conditions plus today's 3-hour forecast for a
fixed city (configured via env) and renders a weather card. Like ``random_xkcd``
it performs its network I/O off the device wake path and degrades to a text
fallback if the API key is missing or a request fails.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional
from zoneinfo import ZoneInfo

import httpx

from ..render.templates import render_weather_html
from .base import ContentItem, ContentKind, Mode, ModeContext

if TYPE_CHECKING:
    from ..config import Settings

logger = logging.getLogger(__name__)

_CURRENT_URL = "https://api.openweathermap.org/data/2.5/weather"
_FORECAST_URL = "https://api.openweathermap.org/data/2.5/forecast"

#: Cache the built weather display dict briefly so the weather card and the
#: artwork overlay can share one fetch instead of each hitting OpenWeather on
#: every refill. Keyed by (city, units, lang).
_WEATHER_TTL = 15 * 60
_weather_cache: dict[tuple[str, str, str], tuple[float, dict]] = {}
_weather_lock = asyncio.Lock()


async def fetch_weather_display(
    http: httpx.AsyncClient, settings: "Settings"
) -> Optional[dict]:
    """Fetch + build the weather display dict, or ``None`` on missing key/error.

    Shared by :class:`WeatherMode` and the artwork overlay; results are cached
    for a few minutes per (city, units, lang) so both callers reuse one fetch.
    """
    if not settings.openweather_api_key:
        return None

    key = (settings.weather_city, settings.weather_units, settings.weather_lang)
    cached = _weather_cache.get(key)
    if cached and (time.time() - cached[0]) < _WEATHER_TTL:
        return cached[1]

    async with _weather_lock:
        cached = _weather_cache.get(key)
        if cached and (time.time() - cached[0]) < _WEATHER_TTL:
            return cached[1]
        params = {
            "q": settings.weather_city,
            "units": settings.weather_units,
            "lang": settings.weather_lang,
            "appid": settings.openweather_api_key,
        }
        try:
            current = (await http.get(_CURRENT_URL, params=params, timeout=15)).json()
            if str(current.get("cod")) != "200":
                raise ValueError(current.get("message", "weather request failed"))
            forecast = None
            try:
                forecast = (
                    await http.get(_FORECAST_URL, params=params, timeout=15)
                ).json()
            except Exception as exc:  # forecast is optional (hi/lo fallback below)
                logger.warning("weather forecast fetch failed: %s", exc)
            data = _build_display(
                current, forecast, settings.timezone, settings.weather_units
            )
        except Exception as exc:
            logger.warning("weather fetch failed: %s", exc)
            return None
        _weather_cache[key] = (time.time(), data)
        return data


class WeatherMode(Mode):
    name = "weather"
    description = "Today's weather for your city."
    periodic = True

    async def generate(self, ctx: ModeContext) -> Optional[ContentItem]:
        cfg = ctx.settings
        if not cfg.openweather_api_key:
            return ContentItem(
                kind=ContentKind.text,
                title="Weather",
                text="Set OPENWEATHER_API_KEY to enable the weather mode.",
            )
        data = await fetch_weather_display(ctx.http, cfg)
        if data is None:
            return ContentItem(
                kind=ContentKind.text,
                title="Weather",
                text="Could not fetch the weather right now.",
            )
        html = render_weather_html(data)
        return ContentItem(kind=ContentKind.html, title="Weather", html=html)


def _build_display(
    current: dict,
    forecast: Optional[dict],
    tz_name: str,
    units: str,
) -> dict:
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)

    main = current.get("main", {})
    weather0 = (current.get("weather") or [{}])[0]
    wind = current.get("wind", {})
    sys_ = current.get("sys", {})

    temp = round(main.get("temp", 0))
    feels = round(main.get("feels_like", main.get("temp", 0)))
    humidity = round(main.get("humidity", 0))
    icon = weather0.get("icon", "01d")
    condition = str(weather0.get("description", "")).title()

    # Today's hi/lo: prefer the forecast (true daily range); fall back to the
    # current reading's min/max if the forecast call failed.
    hi, lo = _today_hi_lo(forecast, tz, now.date())
    if hi is None or lo is None:
        hi = round(main.get("temp_max", temp))
        lo = round(main.get("temp_min", temp))

    # metric wind is m/s -> km/h; imperial is already mph.
    if units == "imperial":
        wind_speed = round(wind.get("speed", 0))
        wind_unit = "mph"
    else:
        wind_speed = round(wind.get("speed", 0) * 3.6)
        wind_unit = "km/h"
    wind_deg = int(wind.get("deg", 0))

    sunrise = _epoch_to_local(sys_.get("sunrise"), tz)
    sunset = _epoch_to_local(sys_.get("sunset"), tz)
    daylight_frac = _daylight_fraction(
        sys_.get("sunrise"), sys_.get("sunset"), now
    )

    city = current.get("name") or ""

    return {
        "city": city,
        "date_str": now.strftime("%a, %b %-d"),
        "temp": temp,
        "feels_like": feels,
        "condition": condition,
        "icon": icon,
        "hi": hi,
        "lo": lo,
        "humidity": humidity,
        "wind_speed": wind_speed,
        "wind_unit": wind_unit,
        "wind_deg": wind_deg,
        "sunrise": sunrise.strftime("%H:%M") if sunrise else "--:--",
        "sunset": sunset.strftime("%H:%M") if sunset else "--:--",
        "daylight_frac": daylight_frac,
        "unit": "\u00b0F" if units == "imperial" else "\u00b0C",
        "now_str": now.strftime("%H:%M"),
    }


def _today_hi_lo(forecast, tz, today) -> tuple[Optional[int], Optional[int]]:
    if not forecast or "list" not in forecast:
        return None, None
    temps: list[float] = []
    for entry in forecast["list"]:
        ts = entry.get("dt")
        if ts is None:
            continue
        local = datetime.fromtimestamp(ts, tz)
        if local.date() == today:
            t = entry.get("main", {}).get("temp")
            if t is not None:
                temps.append(t)
    if not temps:
        return None, None
    return round(max(temps)), round(min(temps))


def _epoch_to_local(epoch, tz) -> Optional[datetime]:
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch, tz)


def _daylight_fraction(sunrise, sunset, now: datetime) -> float:
    """Position of ``now`` between sunrise and sunset, clamped to [0, 1]."""
    if not sunrise or not sunset or sunset <= sunrise:
        return 0.5
    now_epoch = now.astimezone(timezone.utc).timestamp()
    frac = (now_epoch - sunrise) / (sunset - sunrise)
    return max(0.0, min(1.0, frac))
