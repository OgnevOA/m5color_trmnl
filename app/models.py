"""Pydantic models: the device API contract and internal state objects."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class WakeReason(str, Enum):
    timer = "timer"
    button = "button"
    manual = "manual"
    unknown = "unknown"


class DeviceAction(str, Enum):
    draw = "draw"
    sleep = "sleep"
    noop = "noop"
    blank = "blank"


class QueueItemKind(str, Enum):
    text = "text"
    html = "html"
    image = "image"


class QueueItemStatus(str, Enum):
    pending = "pending"
    ready = "ready"
    displayed = "displayed"
    failed = "failed"
    skipped = "skipped"


# --------------------------------------------------------------------------- #
# Device API contract
# --------------------------------------------------------------------------- #
class StatusRequest(BaseModel):
    """Body posted by the device on every wake."""

    battery_percent: Optional[float] = Field(default=None, ge=0, le=100)
    battery_mv: Optional[int] = Field(default=None, ge=0)
    wake_reason: WakeReason = WakeReason.unknown
    last_image_id: Optional[str] = None
    firmware_version: Optional[str] = None
    wifi_rssi: Optional[int] = None

    # Per-cycle timing telemetry (from the previous wake; see firmware). All
    # optional so older firmware that omits them still validates.
    wifi_ms: Optional[int] = Field(default=None, ge=0)
    post_ms: Optional[int] = Field(default=None, ge=0)
    download_ms: Optional[int] = Field(default=None, ge=0)
    draw_ms: Optional[int] = Field(default=None, ge=0)
    awake_ms: Optional[int] = Field(default=None, ge=0)


class ActionResponse(BaseModel):
    """Strict action-based response returned to the device."""

    action: DeviceAction
    image_id: Optional[str] = None
    image_url: Optional[str] = None
    next_wake_seconds: int
    message: Optional[str] = None


# --------------------------------------------------------------------------- #
# Internal state objects
# --------------------------------------------------------------------------- #
class DeviceState(BaseModel):
    device_id: str
    last_battery_percent: Optional[float] = None
    last_battery_mv: Optional[int] = None
    last_wake_reason: Optional[str] = None
    last_wifi_rssi: Optional[int] = None
    last_image_id: Optional[str] = None
    firmware_version: Optional[str] = None
    last_seen: Optional[datetime] = None


class DeviceSettings(BaseModel):
    device_id: str
    interval_minutes: int
    mode: str
    night_mode_enabled: bool = True
    manual_override: bool = False


class QueueItem(BaseModel):
    id: int
    device_id: str
    kind: QueueItemKind
    status: QueueItemStatus
    title: Optional[str] = None
    text_content: Optional[str] = None
    html_content: Optional[str] = None
    source_path: Optional[str] = None
    mode_name: Optional[str] = None
    image_id: Optional[str] = None
    error: Optional[str] = None
    created_at: Optional[datetime] = None
    rendered_at: Optional[datetime] = None


class RenderedImage(BaseModel):
    image_id: str
    device_id: str
    queue_item_id: Optional[int] = None
    path: str
    width: int
    height: int
    created_at: Optional[datetime] = None
    displayed_at: Optional[datetime] = None


class StatusSnapshot(BaseModel):
    """Aggregated status used by the Telegram ``/status`` command."""

    device_id: str
    mode: str
    interval_minutes: int
    night_mode_enabled: bool
    is_night_now: bool
    manual_override: bool
    last_seen: Optional[datetime] = None
    last_wake_reason: Optional[str] = None
    last_image_id: Optional[str] = None
    battery_percent: Optional[float] = None
    queue_pending: int
    queue_ready: int
    #: "home" / "away" / "unknown" when the presence gate is configured, else None.
    presence: Optional[str] = None
