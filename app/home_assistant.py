"""Home Assistant presence lookup for the display presence gate.

Used by the status decision path to decide whether anyone is home. Everything
here fails open: any missing config, timeout, or HTTP error yields ``None``
("unknown"), and the caller treats unknown as "do not gate" so HA downtime can
never freeze the display.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from .config import Settings

logger = logging.getLogger(__name__)

#: Keep the device wake path fast: HA is on the LAN, so this is plenty.
_TIMEOUT_SECONDS = 3.0


async def anyone_home(http: httpx.AsyncClient, settings: Settings) -> Optional[bool]:
    """Return whether at least one presence entity is home.

    ``True``  -> at least one entity reports ``state == "home"``.
    ``False`` -> all entities were reachable and none are home.
    ``None``  -> unconfigured / unreachable / error (caller does not gate).
    """
    if not settings.presence_gating_configured:
        return None

    base = settings.home_assistant_url.rstrip("/")
    headers = {"Authorization": f"Bearer {settings.home_assistant_token}"}

    saw_any = False
    for entity_id in settings.presence_entities:
        try:
            resp = await http.get(
                f"{base}/api/states/{entity_id}",
                headers=headers,
                timeout=_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            state = str(resp.json().get("state", "")).lower()
        except Exception as exc:
            # Fail open: if we cannot read HA reliably, do not gate the display.
            logger.warning("presence check failed for %s: %s", entity_id, exc)
            return None
        saw_any = True
        if state == "home":
            return True

    return False if saw_any else None
