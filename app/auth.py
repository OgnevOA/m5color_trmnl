"""Authentication helpers for device API calls and Telegram users."""

from __future__ import annotations

from .config import Settings
from .db import Database


async def validate_device(db: Database, device_id: str, token: str) -> bool:
    """Return True if the device exists and the bearer token matches."""
    row = await db.fetchone(
        "SELECT token FROM devices WHERE device_id = ?", (device_id,)
    )
    if row is None:
        return False
    return _constant_time_eq(str(row["token"]), token)


def extract_bearer_token(authorization_header: str | None) -> str | None:
    """Extract the token from an ``Authorization: Bearer <token>`` header."""
    if not authorization_header:
        return None
    parts = authorization_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


async def is_user_allowed(db: Database, settings: Settings, user_id: int) -> bool:
    """A Telegram user is allowed if listed in env config or persisted users."""
    if user_id in settings.allowed_user_ids:
        return True
    row = await db.fetchone(
        "SELECT 1 FROM telegram_users WHERE user_id = ?", (user_id,)
    )
    return row is not None


def _constant_time_eq(a: str, b: str) -> bool:
    """Compare two strings in (close to) constant time."""
    if len(a) != len(b):
        return False
    result = 0
    for x, y in zip(a, b):
        result |= ord(x) ^ ord(y)
    return result == 0
