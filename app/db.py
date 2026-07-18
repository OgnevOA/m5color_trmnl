"""SQLite persistence layer built on aiosqlite.

A single :class:`Database` instance owns one connection for the whole process
(the backend, worker, and bot all run in the same asyncio loop), which keeps
SQLite usage simple and avoids multi-process write contention.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Iterable, Optional

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    device_id            TEXT PRIMARY KEY,
    token                TEXT NOT NULL,
    firmware_version     TEXT,
    last_battery_percent REAL,
    last_battery_mv      INTEGER,
    last_wake_reason     TEXT,
    last_wifi_rssi       INTEGER,
    last_image_id        TEXT,
    last_seen            TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    device_id          TEXT PRIMARY KEY,
    interval_minutes   INTEGER NOT NULL,
    mode               TEXT NOT NULL,
    night_mode_enabled INTEGER NOT NULL DEFAULT 1,
    manual_override    INTEGER NOT NULL DEFAULT 0,
    overlay_enabled    INTEGER NOT NULL DEFAULT 0,
    collage_enabled    INTEGER NOT NULL DEFAULT 0,
    collage_count      INTEGER NOT NULL DEFAULT 6,
    FOREIGN KEY (device_id) REFERENCES devices(device_id)
);

CREATE TABLE IF NOT EXISTS queue_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id    TEXT NOT NULL,
    kind         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    title        TEXT,
    text_content TEXT,
    html_content TEXT,
    source_path  TEXT,
    mode_name    TEXT,
    image_id     TEXT,
    error        TEXT,
    created_at   TEXT NOT NULL,
    rendered_at  TEXT
);

CREATE TABLE IF NOT EXISTS rendered_images (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    image_id     TEXT UNIQUE NOT NULL,
    device_id    TEXT NOT NULL,
    queue_item_id INTEGER,
    path         TEXT NOT NULL,
    width        INTEGER NOT NULL,
    height       INTEGER NOT NULL,
    created_at   TEXT NOT NULL,
    displayed_at TEXT,
    render_ms    INTEGER
);

CREATE TABLE IF NOT EXISTS telegram_users (
    user_id  INTEGER PRIMARY KEY,
    added_at TEXT NOT NULL
);

-- User-starred pictures. Each row points at a durable, overlay-free PNG copied
-- out of the render pipeline (see PreRenderWorker's clean-copy capture), so the
-- favorites mode can replay it later with a fresh overlay. Per-device because
-- image ids and panel resolution are per-device.
CREATE TABLE IF NOT EXISTS favorites (
    device_id  TEXT NOT NULL,
    image_id   TEXT NOT NULL,
    title      TEXT,
    path       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (device_id, image_id)
);

CREATE TABLE IF NOT EXISTS event_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id  TEXT,
    level      TEXT NOT NULL,
    kind       TEXT NOT NULL,
    message    TEXT,
    created_at TEXT NOT NULL
);

-- Time-series telemetry: one row per device status POST, for later aggregation
-- (battery curves, wake frequency, draw/noop ratios, RSSI trends, ...).
CREATE TABLE IF NOT EXISTS device_stats (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id         TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    battery_percent   REAL,
    battery_mv        INTEGER,
    wake_reason       TEXT,
    wifi_rssi         INTEGER,
    firmware_version  TEXT,
    mode              TEXT,
    action            TEXT,
    next_wake_seconds INTEGER,
    is_night          INTEGER,
    wifi_ms           INTEGER,
    post_ms           INTEGER,
    download_ms       INTEGER,
    draw_ms           INTEGER,
    awake_ms          INTEGER,
    render_ms         INTEGER
);

CREATE INDEX IF NOT EXISTS idx_queue_status ON queue_items(device_id, status, id);
CREATE INDEX IF NOT EXISTS idx_rendered_device ON rendered_images(device_id, seq);
CREATE INDEX IF NOT EXISTS idx_stats_device_time ON device_stats(device_id, id);
"""


class Database:
    """Thin async wrapper around a single aiosqlite connection."""

    def __init__(self, path: Path | str) -> None:
        self._path = str(path)
        self._conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        if self._conn is not None:
            return
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA foreign_keys=ON;")
        await self._conn.executescript(SCHEMA)
        await self._migrate()
        await self._conn.commit()

    async def _migrate(self) -> None:
        """Apply additive column migrations not covered by CREATE TABLE.

        ``executescript(SCHEMA)`` uses ``CREATE TABLE IF NOT EXISTS``, so columns
        added after a database was first created never appear. Add any missing
        columns idempotently via guarded ``ALTER TABLE``.
        """
        assert self._conn is not None
        additions: dict[str, dict[str, str]] = {
            "settings": {
                "overlay_enabled": "INTEGER",       # 0 / 1 (artwork info overlay)
                "collage_enabled": "INTEGER",        # 0 / 1 (artist collage modifier)
                "collage_count": "INTEGER NOT NULL DEFAULT 6",  # works per collage
            },
            "devices": {
                "battery_alert_state": "TEXT",      # ok / low / critical
                "offline_alerted": "INTEGER",       # 0 / 1
                "expected_next_seconds": "INTEGER", # last wake interval we issued
            },
            "device_stats": {
                "wifi_ms": "INTEGER",
                "post_ms": "INTEGER",
                "download_ms": "INTEGER",
                "draw_ms": "INTEGER",
                "awake_ms": "INTEGER",
                "render_ms": "INTEGER",
            },
            "rendered_images": {
                "render_ms": "INTEGER",
            },
        }
        for table, columns in additions.items():
            cursor = await self._conn.execute(f"PRAGMA table_info({table})")
            existing = {row[1] for row in await cursor.fetchall()}
            await cursor.close()
            for column, col_type in columns.items():
                if column not in existing:
                    await self._conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"
                    )

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected. Call connect() first.")
        return self._conn

    # -- query helpers ----------------------------------------------------- #
    async def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        """Run a write statement, commit, and return ``lastrowid``."""
        async with self._lock:
            cursor = await self.conn.execute(sql, tuple(params))
            await self.conn.commit()
            return cursor.lastrowid

    async def fetchone(
        self, sql: str, params: Iterable[Any] = ()
    ) -> Optional[aiosqlite.Row]:
        cursor = await self.conn.execute(sql, tuple(params))
        row = await cursor.fetchone()
        await cursor.close()
        return row

    async def fetchall(
        self, sql: str, params: Iterable[Any] = ()
    ) -> list[aiosqlite.Row]:
        cursor = await self.conn.execute(sql, tuple(params))
        rows = await cursor.fetchall()
        await cursor.close()
        return list(rows)
