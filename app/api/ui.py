"""Open LAN web control-panel API (``/api/ui/*``).

A thin JSON layer over the existing :class:`~app.services.Services` facade so the
React SPA (served from ``web/dist``) can drive every device: read status and
telemetry, change settings, push content, and preview the next frame.

Unauthenticated on purpose -- the same posture as the ``/stats`` page. It is
meant for a trusted LAN deployment; do not expose it to the public internet.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from .. import queue_service
from ..modes.artist import ArtistMode
from ..modes.registry import available_modes
from ..services import PHOTO_COLLAGE_MAX, Services

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ui", tags=["ui"])


def _services(request: Request, device_id: str) -> Services:
    """Resolve the ``Services`` for a device stack, or 404 if unknown."""
    ctx = request.app.state.ctx
    if not ctx.stacks:
        raise HTTPException(status_code=503, detail="No device stacks running")
    stack = ctx.stacks.get(device_id)
    if stack is None:
        raise HTTPException(status_code=404, detail="Unknown device")
    return stack.services


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #
class ModeBody(BaseModel):
    name: str


class IntervalBody(BaseModel):
    minutes: int


class ToggleBody(BaseModel):
    enabled: bool


class CountBody(BaseModel):
    count: int


class TextBody(BaseModel):
    text: str


class QrBody(BaseModel):
    payload: str


class ImageIdBody(BaseModel):
    image_id: str


# --------------------------------------------------------------------------- #
# Read endpoints
# --------------------------------------------------------------------------- #
@router.get("/meta")
async def meta() -> JSONResponse:
    """Static choices the UI needs to render pickers (modes, collage counts)."""
    return JSONResponse(
        {
            "modes": available_modes(),
            "collage_counts": list(ArtistMode.COLLAGE_COUNTS),
            "collage_count_default": ArtistMode.COLLAGE_COUNT_DEFAULT,
            "photo_collage_max": PHOTO_COLLAGE_MAX,
        }
    )


@router.get("/devices")
async def devices(request: Request) -> JSONResponse:
    """Every device stack running in this process (id + panel type; no tokens)."""
    ctx = request.app.state.ctx
    out = [
        {"device_id": device_id, "device_type": stack.settings.device_type}
        for device_id, stack in ctx.stacks.items()
    ]
    return JSONResponse({"devices": out})


@router.get("/devices/{device_id}/status")
async def device_status(request: Request, device_id: str) -> JSONResponse:
    """Live status + settings snapshot for one device."""
    services = _services(request, device_id)
    snapshot = await services.get_status_snapshot()
    data = snapshot.model_dump(mode="json")
    data["device_type"] = services.settings.device_type
    return JSONResponse(data)


@router.get("/devices/{device_id}/stats")
async def device_stats(
    request: Request,
    device_id: str,
    days: int = Query(default=7, ge=1, le=90),
) -> JSONResponse:
    """Telemetry summary + raw records for the last ``days`` days."""
    services = _services(request, device_id)
    summary = await services.get_stats_summary(hours=days * 24)
    records = await services.get_stats_records(days=days)
    return JSONResponse(
        {"device_id": device_id, "days": days, "summary": summary, "records": records}
    )


@router.get("/devices/{device_id}/preview.png")
async def device_preview(request: Request, device_id: str) -> Response:
    """PNG of the next frame the device will show (E1004 .bin is decoded)."""
    services = _services(request, device_id)
    result = await services.get_next_preview_image()
    if result is None:
        raise HTTPException(status_code=404, detail="No preview available yet")
    data, image_id = result
    return Response(
        content=data,
        media_type="image/png",
        headers={"Cache-Control": "no-store", "X-Image-Id": image_id},
    )


@router.get("/devices/{device_id}/current.png")
async def device_current(request: Request, device_id: str) -> Response:
    """PNG of the frame currently on the device (overlay included; E1004 decoded)."""
    services = _services(request, device_id)
    result = await services.get_current_image()
    if result is None:
        raise HTTPException(status_code=404, detail="No current image yet")
    data, image_id = result
    return Response(
        content=data,
        media_type="image/png",
        headers={"Cache-Control": "no-store", "X-Image-Id": image_id},
    )


# --------------------------------------------------------------------------- #
# Favorites
# --------------------------------------------------------------------------- #
@router.get("/devices/{device_id}/favorites")
async def list_favorites(request: Request, device_id: str) -> JSONResponse:
    services = _services(request, device_id)
    return JSONResponse({"favorites": await services.list_favorites()})


@router.get("/devices/{device_id}/favorites/{image_id}.png")
async def favorite_image(
    request: Request, device_id: str, image_id: str
) -> Response:
    services = _services(request, device_id)
    data = await services.get_favorite_bytes(image_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Unknown favorite")
    return Response(content=data, media_type="image/png")


@router.post("/devices/{device_id}/favorite")
async def add_favorite(
    request: Request, device_id: str, body: ImageIdBody
) -> JSONResponse:
    services = _services(request, device_id)
    ok = await services.add_favorite(body.image_id)
    if not ok:
        raise HTTPException(
            status_code=404, detail="No favoritable image for that id"
        )
    return JSONResponse({"ok": True})


@router.post("/devices/{device_id}/unfavorite")
async def remove_favorite(
    request: Request, device_id: str, body: ImageIdBody
) -> JSONResponse:
    services = _services(request, device_id)
    removed = await services.remove_favorite(body.image_id)
    return JSONResponse({"ok": removed})


# --------------------------------------------------------------------------- #
# Control endpoints
# --------------------------------------------------------------------------- #
@router.post("/devices/{device_id}/mode")
async def set_mode(request: Request, device_id: str, body: ModeBody) -> JSONResponse:
    services = _services(request, device_id)
    known, item_id = await services.select_mode(body.name)
    if not known:
        raise HTTPException(status_code=400, detail=f"Unknown mode: {body.name}")
    return JSONResponse({"ok": True, "queued_item_id": item_id})


@router.post("/devices/{device_id}/interval")
async def set_interval(
    request: Request, device_id: str, body: IntervalBody
) -> JSONResponse:
    services = _services(request, device_id)
    await services.set_interval(body.minutes)
    return JSONResponse({"ok": True})


@router.post("/devices/{device_id}/night")
async def set_night(request: Request, device_id: str, body: ToggleBody) -> JSONResponse:
    services = _services(request, device_id)
    await services.set_night_mode(body.enabled)
    return JSONResponse({"ok": True})


@router.post("/devices/{device_id}/overlay")
async def set_overlay(
    request: Request, device_id: str, body: ToggleBody
) -> JSONResponse:
    services = _services(request, device_id)
    await services.set_overlay(body.enabled)
    return JSONResponse({"ok": True})


@router.post("/devices/{device_id}/collage")
async def set_collage(
    request: Request, device_id: str, body: ToggleBody
) -> JSONResponse:
    services = _services(request, device_id)
    await services.set_collage(body.enabled)
    return JSONResponse({"ok": True})


@router.post("/devices/{device_id}/collage_count")
async def set_collage_count(
    request: Request, device_id: str, body: CountBody
) -> JSONResponse:
    services = _services(request, device_id)
    snapped = await services.set_collage_count(body.count)
    return JSONResponse({"ok": True, "collage_count": snapped})


@router.post("/devices/{device_id}/next")
async def next_item(request: Request, device_id: str) -> JSONResponse:
    services = _services(request, device_id)
    item_id = await services.generate_for_active_mode(force=True)
    return JSONResponse({"ok": True, "queued_item_id": item_id})


@router.post("/devices/{device_id}/skip")
async def skip_item(request: Request, device_id: str) -> JSONResponse:
    services = _services(request, device_id)
    skipped = await queue_service.skip_next(services.db, device_id)
    return JSONResponse({"ok": skipped})


@router.post("/devices/{device_id}/clear")
async def clear_queue(request: Request, device_id: str) -> JSONResponse:
    services = _services(request, device_id)
    cleared = await services.clear_queue()
    return JSONResponse({"ok": True, "cleared": cleared})


@router.post("/devices/{device_id}/text")
async def send_text(request: Request, device_id: str, body: TextBody) -> JSONResponse:
    if not body.text.strip():
        raise HTTPException(status_code=400, detail="Text is empty")
    services = _services(request, device_id)
    item_id = await services.enqueue_user_text(body.text)
    return JSONResponse({"ok": True, "queued_item_id": item_id})


@router.post("/devices/{device_id}/qr")
async def send_qr(request: Request, device_id: str, body: QrBody) -> JSONResponse:
    if not body.payload.strip():
        raise HTTPException(status_code=400, detail="Payload is empty")
    services = _services(request, device_id)
    item_id = await services.enqueue_qr(body.payload)
    return JSONResponse({"ok": True, "queued_item_id": item_id})


@router.post("/devices/{device_id}/image")
async def send_images(
    request: Request,
    device_id: str,
    files: list[UploadFile] = File(...),
) -> JSONResponse:
    """Upload one or more images.

    Mirrors the Telegram album behavior: a single image is shown on its own; a
    batch becomes a face-aware collage when collage mode is on, otherwise an
    image carousel.
    """
    services = _services(request, device_id)
    blobs: list[bytes] = []
    for upload in files[:PHOTO_COLLAGE_MAX]:
        data = await upload.read()
        if data:
            blobs.append(data)
    if not blobs:
        raise HTTPException(status_code=400, detail="No image data received")

    cfg = await services.get_device_settings()
    if len(blobs) == 1:
        item_id, _ = await services.enqueue_user_image(blobs[0])
        return JSONResponse({"ok": True, "kind": "image", "queued_item_id": item_id})

    if cfg.collage_enabled:
        item_id = await services.enqueue_user_collage(blobs)
        return JSONResponse({"ok": True, "kind": "collage", "queued_item_id": item_id})

    # Multiple images, collage off -> group them into one image carousel.
    group = f"web-{uuid.uuid4().hex[:8]}"
    last_id: int | None = None
    for data in blobs:
        last_id, _ = await services.enqueue_user_image(data, media_group_id=group)
    return JSONResponse({"ok": True, "kind": "carousel", "queued_item_id": last_id})
