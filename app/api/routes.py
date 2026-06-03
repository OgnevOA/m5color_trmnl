"""HTTP API routes: device status, image download, and health check."""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request
from fastapi.responses import FileResponse, JSONResponse

from .. import queue_service
from ..auth import extract_bearer_token, validate_device
from ..models import ActionResponse, StatusRequest
from ..services import Services

logger = logging.getLogger(__name__)
router = APIRouter()


def get_services(request: Request) -> Services:
    services: Services = request.app.state.services
    return services


async def require_device(
    device_id: str = Path(...),
    authorization: str | None = Header(default=None),
    services: Services = Depends(get_services),
) -> str:
    """Validate the device id + bearer token, returning the device id."""
    token = extract_bearer_token(authorization)
    if token is None:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    if not await validate_device(services.db, device_id, token):
        raise HTTPException(status_code=401, detail="Invalid device or token")
    return device_id


@router.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@router.post("/api/device/{device_id}/status", response_model=ActionResponse)
async def device_status(
    payload: StatusRequest,
    device_id: str = Depends(require_device),
    services: Services = Depends(get_services),
) -> ActionResponse:
    return await services.handle_status(payload)


@router.get("/api/device/{device_id}/image/{image_id}")
async def device_image(
    image_id: str,
    device_id: str = Depends(require_device),
    services: Services = Depends(get_services),
) -> FileResponse:
    rendered = await queue_service.get_rendered_image(
        services.db, device_id, image_id
    )
    if rendered is None:
        raise HTTPException(status_code=404, detail="Image not found")
    if not os.path.exists(rendered.path):
        raise HTTPException(status_code=404, detail="Image file missing")
    return FileResponse(
        rendered.path,
        media_type="image/png",
        filename=f"{image_id}.png",
    )
