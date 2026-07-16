"""Runtime configuration loaded from environment variables.

All tunable behaviour is driven by environment variables so the system can be
configured cleanly in Docker / docker-compose without code changes.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import time
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

#: Matches per-device env overrides ``DEVICE_<n>_<FIELD>`` (e.g.
#: ``DEVICE_1_DEVICE_ID``, ``DEVICE_2_TELEGRAM_BOT_TOKEN``). ``<FIELD>`` is a
#: ``Settings`` field name; anything else is ignored with a warning.
_DEVICE_ENV_RE = re.compile(r"^DEVICE_(\d+)_(.+)$", re.IGNORECASE)


def _parse_hhmm(value: str) -> time:
    """Parse a ``HH:MM`` string into a :class:`datetime.time`."""
    hours, minutes = value.strip().split(":", 1)
    return time(hour=int(hours), minute=int(minutes))


class Settings(BaseSettings):
    """Application settings.

    Values are read from the process environment (and an optional ``.env`` file
    for local development). Defaults match the deployment spec.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # General
    app_env: str = "production"

    # Storage
    data_dir: Path = Path("/data")
    database_path: Path = Path("/data/trmnl.db")
    rendered_images_dir: Path = Path("/data/rendered")

    # Device identity / auth
    device_id: str = "m5paper-color-01"
    device_token: str = "change-me-device-token"
    #: Panel family driving the render pipeline: "m5" (M5 Paper Color, 400x600
    #: PNG) or "e1004" (reTerminal E1004, 1200x1600 packed .bin frame).
    device_type: str = "m5"

    # Public URL the device uses to build absolute image URLs (optional).
    public_base_url: str = "http://localhost:8000"

    # Telegram
    telegram_bot_token: str = ""
    telegram_allowed_user_ids: str = ""

    # Retention: how many most-recent rendered images to keep per device. Older
    # ones are pruned (DB row + PNG file) after each render. Images still in the
    # active image-mode carousel or currently shown on the device are always
    # kept regardless of this count.
    keep_rendered_images: int = 5

    # Scheduling
    default_interval_minutes: int = 60
    timezone: str = "Asia/Jerusalem"
    night_mode_start: str = "23:00"
    night_mode_end: str = "06:30"

    # Device-health alerts (sent via Telegram). Implicitly enabled when the bot
    # token + allowed_user_ids are set. low_battery_percent triggers a "time to
    # charge" alert; offline_grace_minutes is the slack added on top of the
    # device's expected next check-in before it is considered offline.
    low_battery_percent: int = 20
    offline_grace_minutes: int = 15

    # HTTP server
    host: str = "0.0.0.0"
    port: int = 8000

    # Weather mode (OpenWeatherMap). The "weather" mode is disabled gracefully
    # (shows a hint) until openweather_api_key is set.
    openweather_api_key: str = ""
    weather_city: str = "Tel Aviv,IL"
    weather_units: str = "metric"
    weather_lang: str = "en"

    # Home Assistant presence gate. When configured, the device holds its current
    # image (noop) while nobody is home. The gate is active purely based on
    # configuration: if the URL or token (or entity list) is blank it is skipped
    # entirely. It also fails open on any error so HA downtime never freezes the
    # display. The token grants full control of your home -- keep it in .env only.
    home_assistant_url: str = ""
    home_assistant_token: str = ""
    home_assistant_presence_entities: str = ""
    # "Now Playing" mode: a media_player entity whose artwork is shown as a
    # full-screen poster (e.g. media_player.gostinaia). Reuses the HA url/token.
    home_assistant_media_player_entity: str = ""

    @field_validator("telegram_allowed_user_ids")
    @classmethod
    def _strip_user_ids(cls, value: str) -> str:
        return value.strip()

    @property
    def allowed_user_ids(self) -> set[int]:
        """Parse the comma/space separated allowed Telegram user IDs."""
        raw = self.telegram_allowed_user_ids.replace(",", " ")
        ids: set[int] = set()
        for token in raw.split():
            token = token.strip()
            if not token:
                continue
            try:
                ids.add(int(token))
            except ValueError:
                continue
        return ids

    @property
    def presence_entities(self) -> list[str]:
        """Parse the comma/space separated Home Assistant presence entities."""
        raw = self.home_assistant_presence_entities.replace(",", " ")
        return [token.strip() for token in raw.split() if token.strip()]

    @property
    def presence_gating_configured(self) -> bool:
        """Whether the presence gate has everything it needs to run."""
        return bool(
            self.home_assistant_url
            and self.home_assistant_token
            and self.presence_entities
        )

    @property
    def now_playing_configured(self) -> bool:
        """Whether the Now Playing mode has everything it needs to run."""
        return bool(
            self.home_assistant_url
            and self.home_assistant_token
            and self.home_assistant_media_player_entity
        )

    @property
    def night_start_time(self) -> time:
        return _parse_hhmm(self.night_mode_start)

    @property
    def night_end_time(self) -> time:
        return _parse_hhmm(self.night_mode_end)

    def ensure_directories(self) -> None:
        """Create the data and rendered-images directories if missing."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.rendered_images_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    """Return a cached :class:`Settings` instance."""
    return Settings()


def load_device_settings() -> list[Settings]:
    """Return one :class:`Settings` per device stack to run in this process.

    All configuration lives in the environment (``.env``). Global/shared config
    (timezone, HTTP host/port, mode API keys, Home Assistant, etc.) comes from
    the plain env via :func:`get_settings`. To run several independent devices
    in one process, define ``DEVICE_<n>_<FIELD>`` env vars, where ``<FIELD>`` is
    any :class:`Settings` field name, e.g.::

        DEVICE_1_DEVICE_ID=m5paper-color-01
        DEVICE_1_DEVICE_TYPE=m5
        DEVICE_1_DEVICE_TOKEN=...
        DEVICE_1_TELEGRAM_BOT_TOKEN=111:AAA
        DEVICE_1_DATA_DIR=/data/m5

        DEVICE_2_DEVICE_ID=reterminal-e1004-01
        DEVICE_2_DEVICE_TYPE=e1004
        DEVICE_2_DEVICE_TOKEN=...
        DEVICE_2_TELEGRAM_BOT_TOKEN=222:BBB
        DEVICE_2_DATA_DIR=/data/e1004

    Each index ``n`` yields a ``Settings`` built from the shared base overridden
    by that device's vars (re-validated so strings coerce to ``Path``/``int``).
    ``DEVICE_<n>_DEVICE_ID`` is required per device; missing ``database_path`` /
    ``rendered_images_dir`` are derived from ``data_dir``.

    With no ``DEVICE_<n>_*`` vars this returns ``[get_settings()]`` so existing
    single-device deployments are unchanged.
    """
    base = get_settings()
    valid_fields = set(Settings.model_fields)

    groups: dict[int, dict[str, str]] = {}
    for key, value in os.environ.items():
        match = _DEVICE_ENV_RE.match(key)
        if match is None:
            continue
        idx = int(match.group(1))
        field = match.group(2).lower()
        if field not in valid_fields:
            logger.warning("Ignoring unknown per-device env var %s", key)
            continue
        groups.setdefault(idx, {})[field] = value

    if not groups:
        return [base]

    base_data = base.model_dump()
    result: list[Settings] = []
    seen_ids: set[str] = set()
    for idx in sorted(groups):
        overrides = groups[idx]
        if "device_id" not in overrides:
            logger.warning(
                "Skipping device %d: DEVICE_%d_DEVICE_ID is required", idx, idx
            )
            continue
        data_dir = overrides.get("data_dir")
        if data_dir is not None:
            overrides.setdefault("database_path", str(Path(data_dir) / "trmnl.db"))
            overrides.setdefault(
                "rendered_images_dir", str(Path(data_dir) / "rendered")
            )
        # Re-validate through Settings so string env values coerce to the field
        # types (Path/int); init kwargs take priority over the ambient env.
        settings = Settings(**{**base_data, **overrides})
        if settings.device_id in seen_ids:
            logger.warning(
                "Duplicate device_id %s (device %d); skipping",
                settings.device_id,
                idx,
            )
            continue
        seen_ids.add(settings.device_id)
        result.append(settings)

    return result or [base]
