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
    displayed_at TEXT
);

CREATE TABLE IF NOT EXISTS telegram_users (
    user_id  INTEGER PRIMARY KEY,
    added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id  TEXT,
    level      TEXT NOT NULL,
    kind       TEXT NOT NULL,
    message    TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_queue_status ON queue_items(device_id, status, id);
CREATE INDEX IF NOT EXISTS idx_rendered_device ON rendered_images(device_id, seq);
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
        await self._conn.commit()

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
