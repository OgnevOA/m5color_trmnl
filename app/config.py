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

    # Scheduling
    default_interval_minutes: int = 60
    timezone: str = "Asia/Jerusalem"
    night_mode_start: str = "23:00"
    night_mode_end: str = "06:30"

    # HTTP server
    host: str = "0.0.0.0"
    port: int = 8000

    # Image rendering for e-ink. Photos tend to look dark on the Spectra-6
    # palette, so they are pre-lightened before quantization:
    #   * eink_gamma < 1.0 brightens midtones (1.0 = off).
    #   * eink_autocontrast stretches the tonal range (per-channel).
    eink_gamma: float = 0.75
    eink_autocontrast: bool = True

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
