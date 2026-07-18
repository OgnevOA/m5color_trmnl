"""Content queue management and rendered-image bookkeeping."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .db import Database
from .models import QueueItem, QueueItemKind, QueueItemStatus, RenderedImage

logger = logging.getLogger(__name__)

#: Queue ``mode_name`` values that together make up the user "image" carousel:
#: standalone photos (``image``) plus a composed photo-album collage
#: (``photo_collage`` -- mirrors ``registry.PHOTO_COLLAGE_MODE``; duplicated as a
#: literal here to keep this low-level module free of the heavy modes import).
#: Both are served by the image-mode device path, so the carousel queries must
#: consider them together -- otherwise a freshly rendered collage stays "ready"
#: forever while the device keeps cycling the older plain photos.
_CAROUSEL_MODES = ("image", "photo_collage")
_CAROUSEL_PLACEHOLDERS = ",".join("?" for _ in _CAROUSEL_MODES)


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
    render_ms: Optional[int] = None,
) -> None:
    await db.execute(
        """INSERT INTO rendered_images
           (image_id, device_id, queue_item_id, path, width, height, created_at,
            render_ms)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (image_id, device_id, queue_item_id, path, width, height, _now_iso(),
         render_ms),
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
    """Retire the current image-mode set (photos + collage) so a new batch can
    replace it."""
    rows = await db.fetchall(
        f"""SELECT id FROM queue_items
           WHERE device_id = ? AND mode_name IN ({_CAROUSEL_PLACEHOLDERS})
             AND status IN ('pending','ready','displayed')""",
        (device_id, *_CAROUSEL_MODES),
    )
    await db.execute(
        f"""UPDATE queue_items SET status = 'skipped'
           WHERE device_id = ? AND mode_name IN ({_CAROUSEL_PLACEHOLDERS})
             AND status IN ('pending','ready','displayed')""",
        (device_id, *_CAROUSEL_MODES),
    )
    return len(rows)


async def carousel_images(db: Database, device_id: str) -> list[RenderedImage]:
    """Ordered rendered images of the current image-mode carousel.

    Includes both standalone photos and a composed photo-album collage (see
    ``_CAROUSEL_MODES``) so the image-mode device path can serve either.
    """
    rows = await db.fetchall(
        f"""SELECT r.* FROM rendered_images r
           JOIN queue_items q ON q.id = r.queue_item_id
           WHERE r.device_id = ? AND q.mode_name IN ({_CAROUSEL_PLACEHOLDERS})
             AND q.status IN ('ready','displayed')
           ORDER BY r.seq ASC""",
        (device_id, *_CAROUSEL_MODES),
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


async def latest_rendered_image(
    db: Database, device_id: str
) -> Optional[RenderedImage]:
    """Most recently rendered image for the device (any status)."""
    row = await db.fetchone(
        """SELECT * FROM rendered_images
           WHERE device_id = ? ORDER BY seq DESC LIMIT 1""",
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
async def prune_rendered_images(
    db: Database, device_id: str, keep: int = 5
) -> int:
    """Delete all but the ``keep`` most-recent rendered images for a device.

    Both the ``rendered_images`` row and the PNG file on disk are removed.
    Images that are still in use are always protected regardless of ``keep``:
    the active image-mode carousel set and the device's current ``last_image_id``.
    Returns the number of images deleted.
    """
    if keep < 0:
        return 0
    recent = await db.fetchall(
        """SELECT image_id FROM rendered_images
           WHERE device_id = ? ORDER BY seq DESC LIMIT ?""",
        (device_id, keep),
    )
    protected = {row["image_id"] for row in recent}
    for image in await carousel_images(db, device_id):
        protected.add(image.image_id)
    dev = await db.fetchone(
        "SELECT last_image_id FROM devices WHERE device_id = ?", (device_id,)
    )
    if dev and dev["last_image_id"]:
        protected.add(dev["last_image_id"])

    candidates = await db.fetchall(
        "SELECT image_id, path FROM rendered_images WHERE device_id = ?",
        (device_id,),
    )
    deleted = 0
    for row in candidates:
        if row["image_id"] in protected:
            continue
        if row["path"]:
            rendered = Path(row["path"])
            try:
                rendered.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("could not delete %s: %s", row["path"], exc)
            # Remove the overlay-free clean copy captured beside it (if any), so
            # the favoritable-copy cache prunes in lockstep with the frames.
            clean_sibling = rendered.parent / "clean" / f"{row['image_id']}.png"
            try:
                clean_sibling.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("could not delete %s: %s", clean_sibling, exc)
        await db.execute(
            "DELETE FROM rendered_images WHERE device_id = ? AND image_id = ?",
            (device_id, row["image_id"]),
        )
        deleted += 1
    if deleted:
        logger.info("Pruned %d rendered image(s) for %s", deleted, device_id)
    return deleted


async def prune_source_files(db: Database, device_id: str) -> int:
    """Delete on-disk source/upload files for already-processed queue items.

    Source images (Telegram uploads and periodic-mode downloads in
    ``data_dir/uploads``) are only needed to produce the rendered PNG. Once an
    item leaves ``pending`` (rendered, displayed, skipped or failed) its source
    file is dead weight, so we delete it and clear ``source_path`` so it is not
    revisited. Sources of still-``pending`` items are kept -- the worker still
    needs them to render. Returns the number of files deleted.
    """
    rows = await db.fetchall(
        """SELECT id, source_path FROM queue_items
           WHERE device_id = ? AND source_path IS NOT NULL
             AND status != 'pending'""",
        (device_id,),
    )
    deleted = 0
    for row in rows:
        path = row["source_path"]
        if path:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("could not delete source %s: %s", path, exc)
        await db.execute(
            "UPDATE queue_items SET source_path = NULL WHERE id = ?", (row["id"],)
        )
        deleted += 1
    if deleted:
        logger.info("Pruned %d source file(s) for %s", deleted, device_id)
    return deleted


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
    """Skip the next ready item (preferred) or the next pending item.

    The item is marked ``skipped`` so it is no longer served to the device or
    returned as the next-ready preview. Its rendered row is intentionally left
    in place: image ids are ``MAX(seq)+1``, so deleting it would let the id be
    reused by the next render. Pruning removes it in due course.
    """
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
