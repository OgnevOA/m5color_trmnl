"""Server-authoritative wake scheduling and night-mode logic.

The backend is the single source of truth for when the device should wake.
Night mode is defined as a window (default 23:00-06:30, ``Asia/Jerusalem``)
during which the device sleeps continuously instead of waking repeatedly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class WakePlan:
    """Result of a scheduling decision."""

    next_wake_seconds: int
    is_night: bool


def get_now(timezone: str) -> datetime:
    """Current timezone-aware time in the configured timezone."""
    return datetime.now(ZoneInfo(timezone))


def is_night(now: datetime, start: time, end: time) -> bool:
    """Whether ``now`` falls inside the night window.

    Supports windows that cross midnight (start > end), e.g. 23:00-06:30.
    """
    t = now.time()
    if start <= end:
        return start <= t < end
    return t >= start or t < end


def _next_datetime_at(now: datetime, target: time) -> datetime:
    """First datetime strictly after ``now`` whose clock equals ``target``."""
    candidate = now.replace(
        hour=target.hour, minute=target.minute, second=0, microsecond=0
    )
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def compute_next_wake(
    now: datetime,
    interval_minutes: int,
    night_enabled: bool,
    night_start: time,
    night_end: time,
) -> WakePlan:
    """Calculate the next wake interval in seconds.

    Rules:
      * If currently inside the night window -> sleep until ``night_end``.
      * If the next normal wake would land inside the night window ->
        sleep until ``night_end`` (skip waking during the night).
      * Otherwise -> wake after ``interval_minutes``.
    """
    interval_seconds = max(60, interval_minutes * 60)

    if night_enabled and is_night(now, night_start, night_end):
        target = _next_datetime_at(now, night_end)
        return WakePlan(int((target - now).total_seconds()), True)

    candidate = now + timedelta(seconds=interval_seconds)
    if night_enabled and is_night(candidate, night_start, night_end):
        target = _next_datetime_at(now, night_end)
        return WakePlan(int((target - now).total_seconds()), True)

    return WakePlan(interval_seconds, False)
