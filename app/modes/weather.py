"""Weather-for-today mode backed by OpenWeatherMap.

On each refill it fetches current conditions plus today's 3-hour forecast for a
fixed city (configured via env) and renders a weather card. Like ``random_xkcd``
it performs its network I/O off the device wake path and degrades to a text
fallback if the API key is missing or a request fails.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from ..render.templates import render_weather_html
from .base import ContentItem, ContentKind, Mode, ModeContext

logger = logging.getLogger(__name__)

_CURRENT_URL = "https://api.openweathermap.org/data/2.5/weather"
_FORECAST_URL = "https://api.openweathermap.org/data/2.5/forecast"


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

        params = {
            "q": cfg.weather_city,
            "units": cfg.weather_units,
            "lang": cfg.weather_lang,
            "appid": cfg.openweather_api_key,
        }
        try:
            current = (await ctx.http.get(_CURRENT_URL, params=params, timeout=15)).json()
            if str(current.get("cod")) != "200":
                raise ValueError(current.get("message", "weather request failed"))
            forecast = None
            try:
                forecast = (
                    await ctx.http.get(_FORECAST_URL, params=params, timeout=15)
                ).json()
            except Exception as exc:  # forecast is optional (hi/lo fallback below)
                logger.warning("weather forecast fetch failed: %s", exc)

            data = self._build_display(
                current, forecast, cfg.timezone, cfg.weather_units
            )
            html = render_weather_html(data)
            return ContentItem(kind=ContentKind.html, title="Weather", html=html)
        except Exception as exc:
            logger.warning("weather generation failed: %s", exc)
            return ContentItem(
                kind=ContentKind.text,
                title="Weather",
                text="Could not fetch the weather right now.",
            )

    def _build_display(
        self,
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
        hi, lo = self._today_hi_lo(forecast, tz, now.date())
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

        sunrise = self._epoch_to_local(sys_.get("sunrise"), tz)
        sunset = self._epoch_to_local(sys_.get("sunset"), tz)
        daylight_frac = self._daylight_fraction(
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

    @staticmethod
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

    @staticmethod
    def _epoch_to_local(epoch, tz) -> Optional[datetime]:
        if not epoch:
            return None
        return datetime.fromtimestamp(epoch, tz)

    @staticmethod
    def _daylight_fraction(sunrise, sunset, now: datetime) -> float:
        """Position of ``now`` between sunrise and sunset, clamped to [0, 1]."""
        if not sunrise or not sunset or sunset <= sunrise:
            return 0.5
        now_epoch = now.astimezone(timezone.utc).timestamp()
        frac = (now_epoch - sunrise) / (sunset - sunrise)
        return max(0.0, min(1.0, frac))
