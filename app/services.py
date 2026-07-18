"""Shared service layer used by both the HTTP API and the Telegram bot.

Centralizing this logic keeps the device endpoints, the bot, and the worker in
agreement and avoids duplicating queue/schedule/state handling.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time as _time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Set

import httpx

from . import home_assistant, queue_service
from .config import Settings
from .db import Database
from .models import (
    ActionResponse,
    DeviceAction,
    DeviceSettings,
    StatusRequest,
    StatusSnapshot,
)
from .modes.artist import ArtistMode
from .modes.base import ContentKind, ModeContext
from .modes.favorites import FavoritesMode
from .modes.registry import DEFAULT_MODE, PHOTO_COLLAGE_MODE, get_mode, is_known_mode
from .render.worker import PreRenderWorker
from .scheduler import compute_next_wake, get_now, is_night

logger = logging.getLogger(__name__)

CRITICAL_BATTERY_PERCENT = 5.0
CRITICAL_BATTERY_SLEEP_SECONDS = 6 * 3600

#: Cache the Home Assistant presence result briefly so repeated wakes don't
#: re-hit HA and the device status path stays fast.
PRESENCE_CACHE_TTL_SECONDS = 60.0

#: Photo-album collage tuning. Telegram delivers an album as a rapid burst of
#: separate photo messages with no "end" signal, so we buffer per media group
#: and flush shortly after the last one arrives. The mosaic layouts top out at 9
#: tiles; extra photos are dropped.
ALBUM_DEBOUNCE_SECONDS = 2.5
PHOTO_COLLAGE_MAX = 9
#: Long side (px) each photo is downscaled to before base64-embedding, matching
#: the artist collage tiles (keeps the composed HTML a sane size).
_PHOTO_TILE_PX = 700


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_photo_tile(data: bytes) -> Optional[dict]:
    """Decode one album photo into a collage tile (runs in a worker thread).

    Detects faces (for the face-aware crop), downscales, and base64-encodes the
    image. Returns ``{uri, width, height, face_box}`` where ``face_box`` is the
    normalized union of the faces (or ``None``), or ``None`` if the bytes can't
    be decoded. ``width/height`` are the (EXIF-corrected) original dimensions the
    normalized face box refers to.
    """
    import base64
    import io

    from PIL import Image, ImageOps

    from .render.faces import detect_faces_normalized, union_box

    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img).convert("RGB")
    except Exception:
        return None
    faces = detect_faces_normalized(img)
    box = union_box(faces)
    tile_img = img.copy()
    tile_img.thumbnail((_PHOTO_TILE_PX, _PHOTO_TILE_PX), Image.LANCZOS)
    buf = io.BytesIO()
    tile_img.save(buf, format="JPEG", quality=82, optimize=True)
    uri = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    return {"uri": uri, "width": img.width, "height": img.height, "face_box": box}


def _avg_cycle_drain_mv(series: list[Optional[int]]) -> Optional[float]:
    """Average per-cycle battery drain (mV) from an ordered battery_mv series.

    Only counts consecutive *drops* (drain); charging upticks are ignored so a
    mid-window charge doesn't mask the typical per-cycle consumption. Returns
    ``None`` when there isn't enough data to compute a drop.
    """
    drops: list[int] = []
    prev: Optional[int] = None
    for mv in series:
        if mv is None:
            prev = None
            continue
        if prev is not None and mv < prev:
            drops.append(prev - mv)
        prev = mv
    if not drops:
        return None
    return sum(drops) / len(drops)


class Services:
    """Business logic facade over the database, scheduler, and queue."""

    def __init__(
        self,
        db: Database,
        settings: Settings,
        http: httpx.AsyncClient,
        worker: Optional[PreRenderWorker] = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.http = http
        self.worker = worker
        self.notifier = None  # set via attach_notifier() once the bot exists
        self._uploads_dir = Path(settings.data_dir) / "uploads"
        # Durable, overlay-free copies of starred pictures (survive queue/render
        # pruning) that the favorites mode replays.
        self._favorites_dir = Path(settings.data_dir) / "favorites"
        # Keep strong references to fire-and-forget background tasks.
        self._bg_tasks: Set[asyncio.Task] = set()
        # Image-carousel batching: photos sharing a Telegram media_group_id (an
        # album) accumulate into one carousel; a new group/single image replaces
        # it. The lock serializes concurrent album-photo updates.
        self._carousel_group: Optional[str] = None
        self._carousel_lock = asyncio.Lock()
        # Photo-collage album batching: buffer an album's photos by
        # media_group_id and flush them into one collage shortly after the last
        # one arrives (Telegram sends album photos as separate messages).
        self._album_buffer: dict[str, list[bytes]] = {}
        self._album_timers: dict[str, asyncio.Task] = {}
        self._album_lock = asyncio.Lock()
        # Presence gate: (monotonic_expiry, value) where value is the cached
        # anyone_home result (True/False/None).
        self._presence_cache: Optional[tuple[float, Optional[bool]]] = None

    def attach_worker(self, worker: PreRenderWorker) -> None:
        self.worker = worker

    def attach_notifier(self, notifier) -> None:
        self.notifier = notifier

    async def _notify(self, text: str) -> None:
        """Send a proactive alert if a notifier is attached (best-effort)."""
        if self.notifier is None:
            return
        try:
            await self.notifier.send(text)
        except Exception:
            logger.exception("notification failed")

    # ------------------------------------------------------------------ #
    # Seeding / state
    # ------------------------------------------------------------------ #
    async def seed(self) -> None:
        """Create the device, its settings row, and allowed users if missing."""
        self._uploads_dir.mkdir(parents=True, exist_ok=True)
        self._favorites_dir.mkdir(parents=True, exist_ok=True)
        s = self.settings
        await self.db.execute(
            """INSERT INTO devices (device_id, token)
               VALUES (?, ?)
               ON CONFLICT(device_id) DO UPDATE SET token = excluded.token""",
            (s.device_id, s.device_token),
        )
        await self.db.execute(
            """INSERT OR IGNORE INTO settings
               (device_id, interval_minutes, mode, night_mode_enabled, manual_override)
               VALUES (?, ?, ?, 1, 0)""",
            (s.device_id, s.default_interval_minutes, DEFAULT_MODE),
        )
        for user_id in s.allowed_user_ids:
            await self.db.execute(
                "INSERT OR IGNORE INTO telegram_users (user_id, added_at) VALUES (?, ?)",
                (user_id, _now_iso()),
            )

    async def get_device_settings(self) -> DeviceSettings:
        row = await self.db.fetchone(
            "SELECT * FROM settings WHERE device_id = ?", (self.settings.device_id,)
        )
        if row is None:
            await self.seed()
            row = await self.db.fetchone(
                "SELECT * FROM settings WHERE device_id = ?",
                (self.settings.device_id,),
            )
        return DeviceSettings(
            device_id=row["device_id"],
            interval_minutes=row["interval_minutes"],
            mode=row["mode"],
            night_mode_enabled=bool(row["night_mode_enabled"]),
            manual_override=bool(row["manual_override"]),
            overlay_enabled=bool(row["overlay_enabled"]),
            collage_enabled=bool(row["collage_enabled"]),
            collage_count=int(row["collage_count"] or ArtistMode.COLLAGE_COUNT_DEFAULT),
        )

    async def set_interval(self, minutes: int) -> None:
        minutes = max(1, int(minutes))
        await self.db.execute(
            "UPDATE settings SET interval_minutes = ? WHERE device_id = ?",
            (minutes, self.settings.device_id),
        )

    async def set_mode(self, name: str) -> bool:
        """Set the active mode. Returns True if the mode name is known."""
        known = is_known_mode(name)
        await self.db.execute(
            "UPDATE settings SET mode = ? WHERE device_id = ?",
            (name, self.settings.device_id),
        )
        return known

    async def select_mode(self, name: str) -> tuple[bool, Optional[int]]:
        """Switch the active mode.

        Changing modes overrides (clears) the existing queue. Then:
          * periodic modes (friends, xkcd) immediately generate a first item so
            something renders without a separate ``/next``;
          * static modes (plain_text, image) wait for user-supplied content and
            keep the current display until changed manually.

        Returns ``(known, queued_item_id)``; ``queued_item_id`` is ``None`` for
        static modes.
        """
        known = await self.set_mode(name)
        if not known:
            return False, None
        # A mode change overrides whatever was queued under the previous mode.
        await self.clear_queue()
        mode = get_mode(name)
        if getattr(mode, "periodic", True):
            item_id = await self.generate_for_active_mode(force=True)
            return True, item_id
        return True, None

    def _is_periodic(self, mode_name: str) -> bool:
        return bool(getattr(get_mode(mode_name), "periodic", True))

    def _schedule_periodic_refill(self, mode_name: str) -> None:
        """Fire-and-forget: for periodic modes, ensure the next item is queued.

        Done in the background so the device status request returns immediately
        (no network/generation latency on the device wake path, and never any
        synchronous rendering).
        """
        if not self._is_periodic(mode_name):
            return
        try:
            task = asyncio.create_task(self._refill_safe())
        except RuntimeError:
            # No running event loop (e.g. unit tests) -- skip silently.
            return
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _refill_safe(self) -> None:
        try:
            await self.generate_for_active_mode(force=False)
        except Exception:
            logger.exception("periodic refill failed")

    async def set_night_mode(self, enabled: bool) -> None:
        await self.db.execute(
            "UPDATE settings SET night_mode_enabled = ? WHERE device_id = ?",
            (1 if enabled else 0, self.settings.device_id),
        )

    async def set_overlay(self, enabled: bool) -> None:
        await self.db.execute(
            "UPDATE settings SET overlay_enabled = ? WHERE device_id = ?",
            (1 if enabled else 0, self.settings.device_id),
        )

    async def set_collage(self, enabled: bool) -> None:
        await self.db.execute(
            "UPDATE settings SET collage_enabled = ? WHERE device_id = ?",
            (1 if enabled else 0, self.settings.device_id),
        )

    async def set_collage_count(self, count: int) -> int:
        """Set works-per-collage, snapped to a supported preset. Returns it."""
        snapped = ArtistMode.snap_collage_count(count)
        await self.db.execute(
            "UPDATE settings SET collage_count = ? WHERE device_id = ?",
            (snapped, self.settings.device_id),
        )
        return snapped

    # ------------------------------------------------------------------ #
    # Device status handling (the core scheduling decision)
    # ------------------------------------------------------------------ #
    async def record_status(self, req: StatusRequest) -> None:
        await self.db.execute(
            """UPDATE devices SET
                 last_battery_percent = ?,
                 last_battery_mv = ?,
                 last_wake_reason = ?,
                 last_wifi_rssi = ?,
                 last_image_id = COALESCE(?, last_image_id),
                 firmware_version = COALESCE(?, firmware_version),
                 last_seen = ?
               WHERE device_id = ?""",
            (
                req.battery_percent,
                req.battery_mv,
                req.wake_reason.value,
                req.wifi_rssi,
                req.last_image_id,
                req.firmware_version,
                _now_iso(),
                self.settings.device_id,
            ),
        )

    async def handle_status(self, req: StatusRequest) -> ActionResponse:
        await self.record_status(req)
        cfg = await self.get_device_settings()
        now = get_now(self.settings.timezone)
        response, is_night = await self._decide_action(req, cfg, now)
        await self._record_stats(req, cfg, response, is_night)
        await self._post_status_alerts(req, response)
        return response

    # ------------------------------------------------------------------ #
    # Device-health notifications (battery + offline recovery)
    # ------------------------------------------------------------------ #
    def _battery_bucket(
        self, percent: Optional[float], prev: Optional[str]
    ) -> str:
        """Classify battery into ok/low/critical with clearing hysteresis.

        Entering thresholds: critical <= 5, low <= low_battery_percent.
        Clearing requires extra headroom so a battery hovering at the boundary
        doesn't flap between buckets (and re-fire alerts).
        """
        if percent is None:
            return prev or "ok"
        crit = CRITICAL_BATTERY_PERCENT
        low = float(self.settings.low_battery_percent)
        if percent <= crit:
            return "critical"
        if percent <= low:
            return "low"
        # percent above the low threshold: clear only past a hysteresis margin.
        if prev == "critical" and percent <= crit + 5:
            return "critical"
        if prev == "low" and percent <= low + 5:
            return "low"
        return "ok"

    async def _post_status_alerts(
        self, req: StatusRequest, response: ActionResponse
    ) -> None:
        """Edge-triggered battery alerts, back-online recovery, and bookkeeping.

        Runs after every check-in: persists the wake interval we issued (for the
        offline watcher), notifies on a worsening battery bucket, and clears a
        prior offline alert.
        """
        row = await self.db.fetchone(
            """SELECT battery_alert_state, offline_alerted
                 FROM devices WHERE device_id = ?""",
            (self.settings.device_id,),
        )
        prev_state = (row["battery_alert_state"] if row else None) or "ok"
        was_offline = bool(row["offline_alerted"]) if row else False

        new_state = self._battery_bucket(req.battery_percent, prev_state)
        rank = {"ok": 0, "low": 1, "critical": 2}
        pct = (
            int(round(req.battery_percent))
            if req.battery_percent is not None
            else None
        )
        if rank[new_state] > rank[prev_state]:
            if new_state == "critical":
                await self._notify(
                    f"\u26a0\ufe0f Critical battery ({pct}%) - "
                    "sleeping to conserve power."
                )
            else:
                await self._notify(
                    f"\U0001faab Battery low ({pct}%) - time to charge."
                )
        elif new_state == "ok" and prev_state != "ok":
            await self._notify(f"\u2705 Battery recovered ({pct}%).")

        if was_offline:
            await self._notify("\u2705 Device back online.")

        await self.db.execute(
            """UPDATE devices SET
                 battery_alert_state = ?,
                 offline_alerted = 0,
                 expected_next_seconds = ?
               WHERE device_id = ?""",
            (new_state, response.next_wake_seconds, self.settings.device_id),
        )

    async def check_offline(self) -> None:
        """Alert once when the device misses its expected check-in window."""
        row = await self.db.fetchone(
            """SELECT last_seen, expected_next_seconds, offline_alerted,
                      last_battery_percent
                 FROM devices WHERE device_id = ?""",
            (self.settings.device_id,),
        )
        if row is None or not row["last_seen"] or row["offline_alerted"]:
            return
        try:
            last_seen = datetime.fromisoformat(row["last_seen"])
        except ValueError:
            return
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        expected = row["expected_next_seconds"]
        if expected is None:
            cfg = await self.get_device_settings()
            expected = cfg.interval_minutes * 60
        grace = self.settings.offline_grace_minutes * 60
        elapsed = (datetime.now(timezone.utc) - last_seen).total_seconds()
        if elapsed <= expected + grace:
            return
        local_seen = last_seen.astimezone(get_now(self.settings.timezone).tzinfo)
        pct = row["last_battery_percent"]
        pct_txt = f"{int(round(pct))}%" if pct is not None else "unknown"
        await self._notify(
            "\U0001f4f5 Device offline: no check-in since "
            f"{local_seen.strftime('%H:%M')} (last battery {pct_txt})."
        )
        await self.db.execute(
            "UPDATE devices SET offline_alerted = 1 WHERE device_id = ?",
            (self.settings.device_id,),
        )

    async def run_offline_monitor(self, interval_seconds: float = 300.0) -> None:
        """Background loop: periodically check whether the device went silent."""
        try:
            while True:
                await asyncio.sleep(interval_seconds)
                try:
                    await self.check_offline()
                except Exception:
                    logger.exception("offline check failed")
        except asyncio.CancelledError:
            raise

    async def _decide_action(
        self, req: StatusRequest, cfg: DeviceSettings, now: datetime
    ) -> tuple[ActionResponse, Optional[bool]]:
        """Compute the action for this wake. Returns ``(response, is_night)``."""
        # 1) Critical battery -> sleep long, never render.
        if (
            req.battery_percent is not None
            and req.battery_percent <= CRITICAL_BATTERY_PERCENT
        ):
            return (
                ActionResponse(
                    action=DeviceAction.sleep,
                    next_wake_seconds=CRITICAL_BATTERY_SLEEP_SECONDS,
                    message="Critical battery: sleeping to conserve power.",
                ),
                None,
            )

        plan = compute_next_wake(
            now,
            cfg.interval_minutes,
            cfg.night_mode_enabled,
            self.settings.night_start_time,
            self.settings.night_end_time,
        )

        # 2) Night mode -> sleep through the night, don't touch display.
        if plan.is_night:
            return (
                ActionResponse(
                    action=DeviceAction.sleep,
                    next_wake_seconds=plan.next_wake_seconds,
                    message="Night mode: sleeping until morning.",
                ),
                True,
            )

        # 2b) Presence gate: nobody home -> hold the current image (no refresh).
        # Returning here also skips the periodic refill, avoiding needless
        # weather/XKCD/API calls while away.
        if await self._presence_blocks_update():
            return (
                ActionResponse(
                    action=DeviceAction.noop,
                    next_wake_seconds=plan.next_wake_seconds,
                    message="Nobody home: holding display.",
                ),
                False,
            )

        # 3) Image mode: cycle through the current carousel, one per wake.
        if cfg.mode == "image":
            return await self._handle_image_carousel(req, plan), False

        # 4) Serve a pre-rendered image if one is ready.
        ready = await queue_service.next_ready_image(self.db, self.settings.device_id)
        if ready is not None:
            await queue_service.mark_displayed(
                self.db, self.settings.device_id, ready.image_id
            )
            # Periodic modes show fresh content each wake: pre-render the next
            # item in the background so it is ready for the next wake.
            self._schedule_periodic_refill(cfg.mode)
            return (
                self._draw_response(
                    ready.image_id,
                    plan.next_wake_seconds,
                    epd_mode=get_mode(cfg.mode).epd_mode,
                ),
                False,
            )

        # 5) Nothing new: keep current content (never render synchronously).
        # For periodic modes, also try to refill so the next wake has content.
        self._schedule_periodic_refill(cfg.mode)
        return (
            ActionResponse(
                action=DeviceAction.noop,
                next_wake_seconds=plan.next_wake_seconds,
            ),
            False,
        )

    async def _presence_state(self) -> Optional[bool]:
        """Cached Home Assistant presence (True home / False away / None unknown)."""
        if not self.settings.presence_gating_configured:
            return None
        now = _time.monotonic()
        if self._presence_cache is not None and now < self._presence_cache[0]:
            return self._presence_cache[1]
        value = await home_assistant.anyone_home(self.http, self.settings)
        self._presence_cache = (now + PRESENCE_CACHE_TTL_SECONDS, value)
        return value

    async def _presence_blocks_update(self) -> bool:
        """True only when presence is known and nobody is home (fail open)."""
        return (await self._presence_state()) is False

    async def _record_stats(
        self,
        req: StatusRequest,
        cfg: DeviceSettings,
        response: ActionResponse,
        is_night: Optional[bool],
    ) -> None:
        """Append a telemetry row for this wake (best-effort; never raises)."""
        try:
            # The device timings reported now describe the *previous* cycle, in
            # which the device drew whatever it currently shows (req.last_image_id).
            # Align render_ms to that same image so all of this row's timings
            # (wifi/post/download/draw/render) describe one consistent cycle.
            render_ms = None
            if req.last_image_id:
                row = await self.db.fetchone(
                    "SELECT render_ms FROM rendered_images WHERE image_id = ?",
                    (req.last_image_id,),
                )
                if row is not None:
                    render_ms = row["render_ms"]
            await self.db.execute(
                """INSERT INTO device_stats
                   (device_id, created_at, battery_percent, battery_mv,
                    wake_reason, wifi_rssi, firmware_version, mode, action,
                    next_wake_seconds, is_night,
                    wifi_ms, post_ms, download_ms, draw_ms, awake_ms, render_ms)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    self.settings.device_id,
                    _now_iso(),
                    req.battery_percent,
                    req.battery_mv,
                    req.wake_reason.value,
                    req.wifi_rssi,
                    req.firmware_version,
                    cfg.mode,
                    response.action.value,
                    response.next_wake_seconds,
                    None if is_night is None else (1 if is_night else 0),
                    req.wifi_ms,
                    req.post_ms,
                    req.download_ms,
                    req.draw_ms,
                    req.awake_ms,
                    render_ms,
                ),
            )
        except Exception:
            logger.exception("failed to record device stats")

    async def _handle_image_carousel(self, req: StatusRequest, plan) -> ActionResponse:
        """Advance the image carousel by one each wake, wrapping indefinitely.

        Cursor is stateless: the next image is the one after whatever the device
        currently shows (``req.last_image_id``). A single-image carousel is held
        (noop) once shown, so the e-paper is not needlessly refreshed.
        """
        images = await queue_service.carousel_images(self.db, self.settings.device_id)
        if not images:
            return ActionResponse(
                action=DeviceAction.noop, next_wake_seconds=plan.next_wake_seconds
            )

        ids = [img.image_id for img in images]
        if req.last_image_id in ids:
            nxt = (ids.index(req.last_image_id) + 1) % len(ids)
        else:
            nxt = 0
        chosen = images[nxt]

        # Single image already on screen -> hold (avoid a pointless refresh).
        if chosen.image_id == req.last_image_id:
            return ActionResponse(
                action=DeviceAction.noop, next_wake_seconds=plan.next_wake_seconds
            )

        await queue_service.mark_displayed(
            self.db, self.settings.device_id, chosen.image_id
        )
        return self._draw_response(chosen.image_id, plan.next_wake_seconds)

    def _image_url(self, image_id: str) -> str:
        return f"/api/device/{self.settings.device_id}/image/{image_id}"

    def _draw_response(
        self,
        image_id: str,
        next_wake_seconds: int,
        epd_mode: Optional[str] = None,
    ) -> ActionResponse:
        """Build a ``draw`` response, adapting fields to the device type.

        M5 gets ``image_url`` + an ``epd_mode`` waveform hint. E1004 also gets
        ``frame_url`` (its firmware prefers it) and omits ``epd_mode`` since it
        pushes the packed frame with ``writeNative`` regardless.
        """
        url = self._image_url(image_id)
        is_e1004 = self.settings.device_type == "e1004"
        return ActionResponse(
            action=DeviceAction.draw,
            image_id=image_id,
            image_url=url,
            frame_url=url if is_e1004 else None,
            next_wake_seconds=next_wake_seconds,
            epd_mode=None if is_e1004 else epd_mode,
        )

    async def get_next_preview(self) -> Optional[tuple[str, str]]:
        """Path + image_id of the next image the device will display, if any.

        Falls back to the most recently rendered image so a preview is available
        even when the current item was already shown (e.g. a held static image).
        """
        ready = await queue_service.next_ready_image(self.db, self.settings.device_id)
        if ready is None:
            ready = await queue_service.latest_rendered_image(
                self.db, self.settings.device_id
            )
        if ready is None:
            return None
        path = Path(ready.path)
        if not path.exists():
            return None
        return str(path), ready.image_id

    async def get_next_preview_image(self) -> Optional[tuple[bytes, str]]:
        """PNG bytes + image_id for the next image, for a Telegram preview.

        M5 renders are already PNGs; E1004 renders are packed ``.bin`` frame
        buffers, so decode those back to a PNG so Telegram can display them.
        """
        preview = await self.get_next_preview()
        if preview is None:
            return None
        path, image_id = preview
        data = Path(path).read_bytes()
        if self.settings.device_type == "e1004":
            from .render import e1004

            data = e1004.frame_to_png(data)
        return data, image_id

    async def _resolve_current_rendered(self):
        """The rendered frame physically on the device (or None).

        This is the most recent frame the server handed out with a ``draw``
        action -- its ``displayed_at`` is stamped at send time. The device's own
        reported ``last_image_id`` is deliberately NOT preferred here: the
        firmware POSTs the *previous* frame's id at wake, before drawing the new
        one, so it lags a whole wake behind what is actually on the panel.
        Ordering by ``displayed_at`` also handles carousel re-sends (an older
        image shown again becomes "current"). Both ``current.png`` and the status
        snapshot resolve through here, so the id the panel stars always matches
        the shown frame.
        """
        row = await self.db.fetchone(
            """SELECT image_id FROM rendered_images
               WHERE device_id = ? AND displayed_at IS NOT NULL
               ORDER BY displayed_at DESC, seq DESC LIMIT 1""",
            (self.settings.device_id,),
        )
        if row is not None:
            rendered = await queue_service.get_rendered_image(
                self.db, self.settings.device_id, row["image_id"]
            )
            if rendered is not None:
                return rendered
        # Nothing has been sent for drawing yet: fall back to whatever frame the
        # device last reported (if any).
        dev = await self.db.fetchone(
            "SELECT last_image_id FROM devices WHERE device_id = ?",
            (self.settings.device_id,),
        )
        if dev and dev["last_image_id"]:
            return await queue_service.get_rendered_image(
                self.db, self.settings.device_id, dev["last_image_id"]
            )
        return None

    async def get_current_image(self) -> Optional[tuple[bytes, str]]:
        """PNG bytes + image_id of the frame currently on the device.

        E1004 frames are packed ``.bin`` buffers, so decode those back to PNG
        for the browser. This is the *drawn* frame (overlay included) -- a
        preview, not the overlay-free copy stored for favorites.
        """
        rendered = await self._resolve_current_rendered()
        if rendered is None:
            return None
        path = Path(rendered.path)
        if not path.exists():
            return None
        data = path.read_bytes()
        if self.settings.device_type == "e1004":
            from .render import e1004

            data = e1004.frame_to_png(data)
        return data, rendered.image_id

    # ------------------------------------------------------------------ #
    # Favorites
    # ------------------------------------------------------------------ #
    def _clean_copy_path(self, image_id: str) -> Path:
        """Path of the overlay-free clean copy the worker captured for an image."""
        return Path(self.settings.rendered_images_dir) / "clean" / f"{image_id}.png"

    def _favorite_path(self, image_id: str) -> Path:
        return self._favorites_dir / f"{image_id}.png"

    async def add_favorite(self, image_id: str) -> bool:
        """Star an image: copy its overlay-free clean capture into the durable
        favorites store.

        Returns ``False`` when there is no clean copy for the id (e.g. a flat
        text/QR card, which isn't a picture, or an image already pruned).
        """
        clean = self._clean_copy_path(image_id)
        if not clean.exists():
            return False
        # Title from the queue item that produced the image (e.g. a painting
        # name), so the favorite keeps a caption.
        row = await self.db.fetchone(
            """SELECT q.title AS title FROM rendered_images r
               JOIN queue_items q ON q.id = r.queue_item_id
               WHERE r.device_id = ? AND r.image_id = ?""",
            (self.settings.device_id, image_id),
        )
        title = row["title"] if row else None
        self._favorites_dir.mkdir(parents=True, exist_ok=True)
        dest = self._favorite_path(image_id)
        dest.write_bytes(clean.read_bytes())
        await self.db.execute(
            """INSERT OR IGNORE INTO favorites
               (device_id, image_id, title, path, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (self.settings.device_id, image_id, title, str(dest), _now_iso()),
        )
        return True

    async def remove_favorite(self, image_id: str) -> bool:
        row = await self.db.fetchone(
            "SELECT path FROM favorites WHERE device_id = ? AND image_id = ?",
            (self.settings.device_id, image_id),
        )
        if row is None:
            return False
        if row["path"]:
            try:
                Path(row["path"]).unlink(missing_ok=True)
            except OSError:
                logger.warning("could not delete favorite file %s", row["path"])
        await self.db.execute(
            "DELETE FROM favorites WHERE device_id = ? AND image_id = ?",
            (self.settings.device_id, image_id),
        )
        return True

    async def list_favorites(self) -> list[dict]:
        rows = await self.db.fetchall(
            """SELECT image_id, title, created_at FROM favorites
               WHERE device_id = ? ORDER BY created_at DESC""",
            (self.settings.device_id,),
        )
        return [
            {
                "image_id": r["image_id"],
                "title": r["title"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    async def get_favorite_bytes(self, image_id: str) -> Optional[bytes]:
        row = await self.db.fetchone(
            "SELECT path FROM favorites WHERE device_id = ? AND image_id = ?",
            (self.settings.device_id, image_id),
        )
        path = (
            Path(row["path"]) if row and row["path"] else self._favorite_path(image_id)
        )
        if not path.exists():
            return None
        return path.read_bytes()

    async def _random_favorite(self) -> Optional[tuple[bytes, Optional[str]]]:
        """Bytes + title of a random favorite whose file still exists, or None."""
        rows = await self.db.fetchall(
            "SELECT title, path FROM favorites WHERE device_id = ?",
            (self.settings.device_id,),
        )
        usable = [r for r in rows if r["path"] and Path(r["path"]).exists()]
        if not usable:
            return None
        choice = random.choice(usable)
        return Path(choice["path"]).read_bytes(), choice["title"]

    async def get_stats_summary(self, hours: int = 24) -> dict:
        """Aggregate recent telemetry for a quick at-a-glance summary."""
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        # Timings describe the *previous* cycle (one-cycle lag), so we bucket them
        # by whether that cycle actually drew (draw_ms > 0) rather than by this
        # row's `action`. A reporting row is one that carried metrics (awake_ms).
        row = await self.db.fetchone(
            """SELECT COUNT(*) AS samples,
                      MIN(battery_percent) AS bat_min,
                      MAX(battery_percent) AS bat_max,
                      AVG(battery_percent) AS bat_avg,
                      AVG(wifi_rssi) AS rssi_avg,
                      AVG(wifi_ms) AS wifi_ms_avg,
                      AVG(post_ms) AS post_ms_avg,
                      SUM(CASE WHEN draw_ms > 0 THEN 1 ELSE 0 END) AS draw_cycles,
                      SUM(CASE WHEN awake_ms IS NOT NULL AND COALESCE(draw_ms, 0) = 0
                               THEN 1 ELSE 0 END) AS idle_cycles,
                      AVG(CASE WHEN draw_ms > 0 THEN awake_ms END) AS draw_awake_avg,
                      AVG(CASE WHEN draw_ms > 0 THEN draw_ms END) AS draw_ms_avg,
                      AVG(CASE WHEN draw_ms > 0 THEN download_ms END) AS draw_download_avg,
                      AVG(CASE WHEN draw_ms > 0 THEN render_ms END) AS draw_render_avg,
                      AVG(CASE WHEN awake_ms IS NOT NULL AND COALESCE(draw_ms, 0) = 0
                               THEN awake_ms END) AS idle_awake_avg,
                      SUM(CASE WHEN action = 'draw' THEN 1 ELSE 0 END) AS draws,
                      SUM(CASE WHEN action = 'sleep' THEN 1 ELSE 0 END) AS sleeps,
                      SUM(CASE WHEN action = 'noop' THEN 1 ELSE 0 END) AS noops
               FROM device_stats
               WHERE device_id = ? AND created_at >= ?""",
            (self.settings.device_id, since),
        )
        total = await self.db.fetchone(
            "SELECT COUNT(*) AS c FROM device_stats WHERE device_id = ?",
            (self.settings.device_id,),
        )
        mv_rows = await self.db.fetchall(
            """SELECT battery_mv FROM device_stats
               WHERE device_id = ? AND created_at >= ? AND battery_mv IS NOT NULL
               ORDER BY id ASC""",
            (self.settings.device_id, since),
        )
        battery_drain_mv = _avg_cycle_drain_mv([r["battery_mv"] for r in mv_rows])
        return {
            "hours": hours,
            "samples": int(row["samples"]) if row else 0,
            "total_samples": int(total["c"]) if total else 0,
            "battery_min": row["bat_min"] if row else None,
            "battery_max": row["bat_max"] if row else None,
            "battery_avg": row["bat_avg"] if row else None,
            "battery_drain_mv": battery_drain_mv,
            "rssi_avg": row["rssi_avg"] if row else None,
            "wifi_ms_avg": row["wifi_ms_avg"] if row else None,
            "post_ms_avg": row["post_ms_avg"] if row else None,
            # Timings split by whether the (previous) cycle drew or held.
            "draw_cycles": int(row["draw_cycles"] or 0) if row else 0,
            "idle_cycles": int(row["idle_cycles"] or 0) if row else 0,
            "draw_awake_avg": row["draw_awake_avg"] if row else None,
            "draw_ms_avg": row["draw_ms_avg"] if row else None,
            "draw_download_avg": row["draw_download_avg"] if row else None,
            "draw_render_avg": row["draw_render_avg"] if row else None,
            "idle_awake_avg": row["idle_awake_avg"] if row else None,
            "draws": int(row["draws"] or 0) if row else 0,
            "sleeps": int(row["sleeps"] or 0) if row else 0,
            "noops": int(row["noops"] or 0) if row else 0,
        }

    async def get_stats_records(
        self, days: int = 7, limit: int = 5000
    ) -> list[dict]:
        """Raw telemetry rows for the last ``days`` days, newest first."""
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = await self.db.fetchall(
            """SELECT created_at, action, mode, wake_reason,
                      battery_percent, battery_mv, wifi_rssi, firmware_version,
                      next_wake_seconds, is_night,
                      wifi_ms, post_ms, download_ms, draw_ms, awake_ms, render_ms
               FROM device_stats
               WHERE device_id = ? AND created_at >= ?
               ORDER BY id DESC LIMIT ?""",
            (self.settings.device_id, since, limit),
        )
        return [{key: row[key] for key in row.keys()} for row in rows]

    # ------------------------------------------------------------------ #
    # Status snapshot for the bot
    # ------------------------------------------------------------------ #
    async def get_status_snapshot(self) -> StatusSnapshot:
        cfg = await self.get_device_settings()
        dev = await self.db.fetchone(
            "SELECT * FROM devices WHERE device_id = ?", (self.settings.device_id,)
        )
        now = get_now(self.settings.timezone)
        night_now = cfg.night_mode_enabled and is_night(
            now, self.settings.night_start_time, self.settings.night_end_time
        )
        last_seen = None
        if dev is not None and dev["last_seen"]:
            try:
                last_seen = datetime.fromisoformat(dev["last_seen"])
            except ValueError:
                last_seen = None
        presence = None
        if self.settings.presence_gating_configured:
            state = await self._presence_state()
            presence = {True: "home", False: "away"}.get(state, "unknown")
        current = await self._resolve_current_rendered()
        current_image_id = current.image_id if current else None
        next_preview = await self.get_next_preview()
        next_image_id = next_preview[1] if next_preview else None
        return StatusSnapshot(
            device_id=self.settings.device_id,
            mode=cfg.mode,
            interval_minutes=cfg.interval_minutes,
            night_mode_enabled=cfg.night_mode_enabled,
            is_night_now=night_now,
            manual_override=cfg.manual_override,
            overlay_enabled=cfg.overlay_enabled,
            collage_enabled=cfg.collage_enabled,
            collage_count=cfg.collage_count,
            last_seen=last_seen,
            last_wake_reason=dev["last_wake_reason"] if dev else None,
            last_image_id=current_image_id,
            next_image_id=next_image_id,
            battery_percent=dev["last_battery_percent"] if dev else None,
            queue_pending=await queue_service.count_pending(
                self.db, self.settings.device_id
            ),
            queue_ready=await queue_service.count_ready(
                self.db, self.settings.device_id
            ),
            presence=presence,
        )

    # ------------------------------------------------------------------ #
    # Content input
    # ------------------------------------------------------------------ #
    async def _switch_to_static_mode(self, target: str) -> None:
        """Switch to a static mode for user-supplied content.

        Sending content implicitly selects the matching static mode. If we are
        coming from a different mode (e.g. a periodic one like
        ``random_friends_quote``), clear the queue first so stale auto-generated
        items don't compete with what the user just sent.
        """
        cfg = await self.get_device_settings()
        if cfg.mode != target:
            await self.clear_queue()
            await self.set_mode(target)

    async def enqueue_user_text(self, text: str) -> int:
        await self._switch_to_static_mode("plain_text")
        item_id = await queue_service.add_text_item(
            self.db, self.settings.device_id, text=text, title="Message"
        )
        self._notify_worker()
        return item_id

    async def enqueue_qr(self, payload: str) -> int:
        """Switch to the QR mode and queue a QR code encoding ``payload``."""
        from .render.templates import render_qr_html

        await self._switch_to_static_mode("qr")
        html = render_qr_html(payload)
        item_id = await queue_service.add_html_item(
            self.db,
            self.settings.device_id,
            html=html,
            title="QR Code",
            mode_name="qr",
        )
        self._notify_worker()
        return item_id

    async def enqueue_user_image(
        self,
        data: bytes,
        suffix: str = ".jpg",
        media_group_id: Optional[str] = None,
    ) -> tuple[int, bool]:
        """Queue a user image into the image-mode carousel.

        Photos of the same Telegram album (``media_group_id``) accumulate into a
        single carousel; a new album or a standalone image replaces the previous
        set. Returns ``(item_id, started_new_carousel)``.
        """
        async with self._carousel_lock:
            await self._switch_to_static_mode("image")
            started_new = media_group_id is None or media_group_id != self._carousel_group
            if started_new:
                await queue_service.reset_image_carousel(
                    self.db, self.settings.device_id
                )
                self._carousel_group = media_group_id
            self._uploads_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
            path = self._uploads_dir / f"upload_{ts}{suffix}"
            path.write_bytes(data)
            item_id = await queue_service.add_image_item(
                self.db,
                self.settings.device_id,
                source_path=str(path),
                title="Image",
                mode_name="image",
            )
        self._notify_worker()
        return item_id, started_new

    async def add_album_photo(self, media_group_id: str, data: bytes) -> bool:
        """Buffer one photo of an album for a face-aware collage.

        Photos of a Telegram album arrive as separate messages; we accumulate
        them by ``media_group_id`` and (re)arm a short debounce timer that flushes
        the buffer into one collage once the burst settles. Returns ``True`` for
        the first photo of the album (so the caller replies just once).
        """
        async with self._album_lock:
            buf = self._album_buffer.setdefault(media_group_id, [])
            started = len(buf) == 0
            if len(buf) < PHOTO_COLLAGE_MAX:
                buf.append(data)
            timer = self._album_timers.get(media_group_id)
            if timer is not None and not timer.done():
                timer.cancel()
            self._album_timers[media_group_id] = asyncio.create_task(
                self._album_flush_after(media_group_id)
            )
        return started

    async def _album_flush_after(self, media_group_id: str) -> None:
        try:
            await asyncio.sleep(ALBUM_DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return  # another photo arrived; a fresh timer was armed
        async with self._album_lock:
            images = self._album_buffer.pop(media_group_id, [])
            self._album_timers.pop(media_group_id, None)
        if not images:
            return
        try:
            await self.enqueue_user_collage(images)
        except Exception:
            logger.exception("photo collage build failed")

    def _device_size(self) -> tuple[int, int]:
        """Frame size (w, h) for this device, matching the worker's output."""
        from .render import e1004, image_ops

        if self.settings.device_type == "e1004":
            return e1004.E1004_WIDTH, e1004.E1004_HEIGHT
        return image_ops.TARGET_WIDTH, image_ops.TARGET_HEIGHT

    async def enqueue_user_collage(self, images: list[bytes]) -> Optional[int]:
        """Compose several user photos into one face-aware collage frame.

        Each photo is decoded, face-detected and downscaled off the event loop
        (``to_thread``); the mosaic is rendered to HTML and enqueued as a static
        display. Falls back to a single image if fewer than two photos decode.
        """
        from .render.templates import render_collage_html

        images = images[:PHOTO_COLLAGE_MAX]
        tiles = await asyncio.gather(
            *(asyncio.to_thread(_build_photo_tile, d) for d in images)
        )
        tiles = [t for t in tiles if t]
        if len(tiles) < 2:
            if images:
                await self.enqueue_user_image(images[0])
            return None

        html = render_collage_html(
            "", tiles, photo=True, device_size=self._device_size()
        )
        async with self._carousel_lock:
            await self._switch_to_static_mode("image")
            # Retire the whole existing photo carousel -- including already
            # *displayed* photos, which ``clear_queue`` leaves untouched. The
            # collage is served by the image-mode carousel path, so any leftover
            # displayed photo would otherwise keep cycling in front of it (the
            # "shows a previous picture" bug).
            await queue_service.reset_image_carousel(self.db, self.settings.device_id)
            self._carousel_group = None  # the collage replaces any carousel
            item_id = await queue_service.add_html_item(
                self.db,
                self.settings.device_id,
                html=html,
                title="Collage",
                mode_name=PHOTO_COLLAGE_MODE,
            )
        self._notify_worker()
        return item_id

    async def generate_for_active_mode(self, force: bool = False) -> Optional[int]:
        """Generate the next item for the active mode.

        With ``force=False`` (``/next``), generation is skipped when an item is
        already queued/ready. With ``force=True`` (e.g. on mode change) an item
        is always generated.
        """
        if not force:
            pending = await queue_service.count_pending(
                self.db, self.settings.device_id
            )
            ready = await queue_service.count_ready(
                self.db, self.settings.device_id
            )
            if pending + ready > 0:
                return None

        cfg = await self.get_device_settings()
        mode = get_mode(cfg.mode)
        ctx = ModeContext(http=self.http, settings=self.settings)
        # Favorites replays a starred picture; selection needs DB + filesystem,
        # so it lives here (like the collage modifier) rather than in the mode.
        if isinstance(mode, FavoritesMode):
            favorite = await self._random_favorite()
            if favorite is not None:
                data, title = favorite
                return await self._enqueue_mode_image(
                    data, cfg.mode, title=title or "Favorite"
                )
            # No favorites yet -> fall through to the mode's text fallback.
            content = await mode.generate(ctx)
        # The collage modifier turns any artist mode into a mosaic of several
        # works; other modes ignore it.
        elif cfg.collage_enabled and isinstance(mode, ArtistMode):
            content = await mode.generate_collage(ctx, cfg.collage_count)
        else:
            content = await mode.generate(ctx)
        if content is None:
            return None

        if content.kind == ContentKind.image and content.image_bytes:
            return await self._enqueue_mode_image(
                content.image_bytes, cfg.mode, title=content.title
            )
        if content.kind == ContentKind.html and content.html:
            item_id = await queue_service.add_html_item(
                self.db,
                self.settings.device_id,
                html=content.html,
                title=content.title,
                mode_name=cfg.mode,
            )
        else:
            item_id = await queue_service.add_text_item(
                self.db,
                self.settings.device_id,
                text=content.text or "",
                title=content.title,
                mode_name=cfg.mode,
            )
        self._notify_worker()
        return item_id

    async def _enqueue_mode_image(
        self, data: bytes, mode_name: str, title: Optional[str] = None
    ) -> int:
        self._uploads_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        path = self._uploads_dir / f"{mode_name}_{ts}.png"
        path.write_bytes(data)
        item_id = await queue_service.add_image_item(
            self.db,
            self.settings.device_id,
            source_path=str(path),
            # Keep the content's real title (e.g. a painting name) so the overlay
            # can caption it; fall back to the mode name when absent.
            title=title or mode_name,
            mode_name=mode_name,
        )
        self._notify_worker()
        return item_id

    async def clear_queue(self) -> int:
        return await queue_service.clear_pending(self.db, self.settings.device_id)

    def _notify_worker(self) -> None:
        if self.worker is not None:
            self.worker.notify()
