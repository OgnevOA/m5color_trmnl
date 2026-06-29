"""Runtime configuration loaded from environment variables.

All tunable behaviour is driven by environment variables so the system can be
configured cleanly in Docker / docker-compose without code changes.
"""

from __future__ import annotations

from datetime import time
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # Metropolitan Museum of Art mode (no API key required). met_search_query is
    # an optional full-text filter; blank means "random across the whole
    # public-domain collection" (set e.g. "monet" or "ukiyo-e" to curate).
    met_search_query: str = ""

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
