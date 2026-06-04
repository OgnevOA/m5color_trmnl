"""Content queue management and rendered-image bookkeeping."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .db import Database
from .models import QueueItem, QueueItemKind, QueueItemStatus, RenderedImage


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_queue_item(row) -> QueueItem:
    return QueueItem(
        id=row["id"],
        device_id=row["device_id"],
        kind=QueueItemKind(row["kind"]),
        status=QueueItemStatus(row["status"]),
        title=row["title"],
        text_content=row["text_content"],
        html_content=row["html_content"],
        source_path=row["source_path"],
        mode_name=row["mode_name"],
        image_id=row["image_id"],
        error=row["error"],
    )


def _row_to_rendered(row) -> RenderedImage:
    return RenderedImage(
        image_id=row["image_id"],
        device_id=row["device_id"],
        queue_item_id=row["queue_item_id"],
        path=row["path"],
        width=row["width"],
        height=row["height"],
    )


# --------------------------------------------------------------------------- #
# Enqueue
# --------------------------------------------------------------------------- #
async def add_text_item(
    db: Database, device_id: str, text: str, title: str = "Message", mode_name: str | None = None
) -> int:
    return await db.execute(
        """INSERT INTO queue_items
           (device_id, kind, status, title, text_content, mode_name, created_at)
           VALUES (?, ?, 'pending', ?, ?, ?, ?)""",
        (device_id, QueueItemKind.text.value, title, text, mode_name, _now_iso()),
    )


async def add_html_item(
    db: Database, device_id: str, html: str, title: str = "Message", mode_name: str | None = None
) -> int:
    return await db.execute(
        """INSERT INTO queue_items
           (device_id, kind, status, title, html_content, mode_name, created_at)
           VALUES (?, ?, 'pending', ?, ?, ?, ?)""",
        (device_id, QueueItemKind.html.value, title, html, mode_name, _now_iso()),
    )


async def add_image_item(
    db: Database, device_id: str, source_path: str, title: str = "Image", mode_name: str | None = None
) -> int:
    return await db.execute(
        """INSERT INTO queue_items
           (device_id, kind, status, title, source_path, mode_name, created_at)
           VALUES (?, ?, 'pending', ?, ?, ?, ?)""",
        (device_id, QueueItemKind.image.value, title, source_path, mode_name, _now_iso()),
    )


# --------------------------------------------------------------------------- #
# Worker-facing helpers
# --------------------------------------------------------------------------- #
async def next_pending(db: Database, device_id: str) -> Optional[QueueItem]:
    row = await db.fetchone(
        """SELECT * FROM queue_items
           WHERE device_id = ? AND status = 'pending'
           ORDER BY id ASC LIMIT 1""",
        (device_id,),
    )
    return _row_to_queue_item(row) if row else None


async def count_ready(db: Database, device_id: str) -> int:
    row = await db.fetchone(
        "SELECT COUNT(*) AS c FROM queue_items WHERE device_id = ? AND status = 'ready'",
        (device_id,),
    )
    return int(row["c"]) if row else 0


async def count_pending(db: Database, device_id: str) -> int:
    row = await db.fetchone(
        "SELECT COUNT(*) AS c FROM queue_items WHERE device_id = ? AND status = 'pending'",
        (device_id,),
    )
    return int(row["c"]) if row else 0


async def next_image_id(db: Database) -> str:
    row = await db.fetchone("SELECT COALESCE(MAX(seq), 0) AS m FROM rendered_images")
    next_seq = (int(row["m"]) if row else 0) + 1
    return f"img_{next_seq:06d}"


async def record_rendered(
    db: Database,
    device_id: str,
    queue_item_id: int,
    image_id: str,
    path: str,
    width: int,
    height: int,
) -> None:
    await db.execute(
        """INSERT INTO rendered_images
           (image_id, device_id, queue_item_id, path, width, height, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (image_id, device_id, queue_item_id, path, width, height, _now_iso()),
    )
    await db.execute(
        "UPDATE queue_items SET status = 'ready', image_id = ?, rendered_at = ? WHERE id = ?",
        (image_id, _now_iso(), queue_item_id),
    )


async def mark_failed(db: Database, queue_item_id: int, error: str) -> None:
    await db.execute(
        "UPDATE queue_items SET status = 'failed', error = ? WHERE id = ?",
        (error[:500], queue_item_id),
    )


# --------------------------------------------------------------------------- #
# Image carousel (image mode)
# --------------------------------------------------------------------------- #
async def reset_image_carousel(db: Database, device_id: str) -> int:
    """Retire the current image-mode set so a new batch can replace it."""
    rows = await db.fetchall(
        """SELECT id FROM queue_items
           WHERE device_id = ? AND mode_name = 'image'
             AND status IN ('pending','ready','displayed')""",
        (device_id,),
    )
    await db.execute(
        """UPDATE queue_items SET status = 'skipped'
           WHERE device_id = ? AND mode_name = 'image'
             AND status IN ('pending','ready','displayed')""",
        (device_id,),
    )
    return len(rows)


async def carousel_images(db: Database, device_id: str) -> list[RenderedImage]:
    """Ordered rendered images of the current image-mode carousel."""
    rows = await db.fetchall(
        """SELECT r.* FROM rendered_images r
           JOIN queue_items q ON q.id = r.queue_item_id
           WHERE r.device_id = ? AND q.mode_name = 'image'
             AND q.status IN ('ready','displayed')
           ORDER BY r.seq ASC""",
        (device_id,),
    )
    return [_row_to_rendered(row) for row in rows]


# --------------------------------------------------------------------------- #
# Device-facing helpers
# --------------------------------------------------------------------------- #
async def next_ready_image(db: Database, device_id: str) -> Optional[RenderedImage]:
    """Oldest rendered image whose queue item is ready (not yet displayed)."""
    row = await db.fetchone(
        """SELECT r.* FROM rendered_images r
           JOIN queue_items q ON q.id = r.queue_item_id
           WHERE r.device_id = ? AND q.status = 'ready'
           ORDER BY r.seq ASC LIMIT 1""",
        (device_id,),
    )
    return _row_to_rendered(row) if row else None


async def get_rendered_image(
    db: Database, device_id: str, image_id: str
) -> Optional[RenderedImage]:
    row = await db.fetchone(
        "SELECT * FROM rendered_images WHERE device_id = ? AND image_id = ?",
        (device_id, image_id),
    )
    return _row_to_rendered(row) if row else None


async def mark_displayed(db: Database, device_id: str, image_id: str) -> None:
    await db.execute(
        "UPDATE rendered_images SET displayed_at = ? WHERE device_id = ? AND image_id = ?",
        (_now_iso(), device_id, image_id),
    )
    await db.execute(
        """UPDATE queue_items SET status = 'displayed'
           WHERE device_id = ? AND image_id = ? AND status = 'ready'""",
        (device_id, image_id),
    )


# --------------------------------------------------------------------------- #
# Management
# --------------------------------------------------------------------------- #
async def clear_pending(db: Database, device_id: str) -> int:
    rows = await db.fetchall(
        "SELECT id FROM queue_items WHERE device_id = ? AND status IN ('pending','ready')",
        (device_id,),
    )
    await db.execute(
        """UPDATE queue_items SET status = 'skipped'
           WHERE device_id = ? AND status IN ('pending','ready')""",
        (device_id,),
    )
    return len(rows)


async def skip_next(db: Database, device_id: str) -> bool:
    """Skip the next ready item (preferred) or the next pending item."""
    row = await db.fetchone(
        """SELECT id FROM queue_items
           WHERE device_id = ? AND status = 'ready'
           ORDER BY id ASC LIMIT 1""",
        (device_id,),
    )
    if row is None:
        row = await db.fetchone(
            """SELECT id FROM queue_items
               WHERE device_id = ? AND status = 'pending'
               ORDER BY id ASC LIMIT 1""",
            (device_id,),
        )
    if row is None:
        return False
    await db.execute(
        "UPDATE queue_items SET status = 'skipped' WHERE id = ?", (row["id"],)
    )
    return True
