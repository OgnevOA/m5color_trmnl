"""HTTP API routes: device status, image download, and health check."""

from __future__ import annotations

import html
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from .. import queue_service
from ..auth import extract_bearer_token, validate_device
from ..config import Settings
from ..models import ActionResponse, StatusRequest
from ..services import Services

logger = logging.getLogger(__name__)
router = APIRouter()


def _services_for(request: Request, device_id: str) -> Services:
    """Resolve the ``Services`` for a device stack, or 404 if unknown."""
    ctx = request.app.state.ctx
    stack = ctx.stacks.get(device_id)
    if stack is None:
        raise HTTPException(status_code=404, detail="Unknown device")
    return stack.services


def device_services(
    request: Request, device_id: str = Path(...)
) -> Services:
    """Dependency: the ``Services`` for the ``{device_id}`` in the URL path."""
    return _services_for(request, device_id)


async def require_device(
    request: Request,
    device_id: str = Path(...),
    authorization: str | None = Header(default=None),
) -> str:
    """Validate the device id + bearer token against that stack, returning the id."""
    token = extract_bearer_token(authorization)
    if token is None:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    services = _services_for(request, device_id)
    if not await validate_device(services.db, device_id, token):
        raise HTTPException(status_code=401, detail="Invalid device or token")
    return device_id


@router.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@router.get("/stats")
async def stats(
    request: Request,
    days: int = Query(default=7, ge=1, le=90),
    format: str = Query(default="html", pattern="^(html|json)$"),
    device: str | None = Query(default=None),
):
    """Browser-friendly telemetry view for the last N days.

    Open ``/stats`` for an HTML table, or ``/stats?days=30&format=json`` for raw
    records. Pick a device with ``?device=<id>`` (defaults to the first stack)
    when more than one is configured. Unauthenticated: intended for the
    LAN-local deployment.
    """
    ctx = request.app.state.ctx
    if not ctx.stacks:
        raise HTTPException(status_code=503, detail="No device stacks running")
    if device is None:
        device = next(iter(ctx.stacks))
    services = _services_for(request, device)
    summary = await services.get_stats_summary(hours=days * 24)
    records = await services.get_stats_records(days=days)
    if format == "json":
        return JSONResponse(
            {
                "device_id": services.settings.device_id,
                "days": days,
                "count": len(records),
                "summary": summary,
                "records": records,
            }
        )
    return HTMLResponse(_render_stats_html(services.settings, days, summary, records))


def _render_stats_html(
    settings: Settings, days: int, summary: dict, records: list[dict]
) -> str:
    tz = ZoneInfo(settings.timezone)

    def localtime(iso: str | None) -> str:
        if not iso:
            return ""
        try:
            return datetime.fromisoformat(iso).astimezone(tz).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        except ValueError:
            return iso

    def cell(value: object) -> str:
        if value is None:
            return '<td class="muted">-</td>'
        if isinstance(value, float):
            return f"<td>{value:.0f}</td>"
        return f"<td>{html.escape(str(value))}</td>"

    columns = [
        ("Time", lambda r: localtime(r.get("created_at"))),
        ("Action", lambda r: r.get("action")),
        ("Mode", lambda r: r.get("mode")),
        ("Wake", lambda r: r.get("wake_reason")),
        ("Batt %", lambda r: r.get("battery_percent")),
        ("Batt mV", lambda r: r.get("battery_mv")),
        ("RSSI", lambda r: r.get("wifi_rssi")),
        ("WiFi ms", lambda r: r.get("wifi_ms")),
        ("POST ms", lambda r: r.get("post_ms")),
        ("DL ms", lambda r: r.get("download_ms")),
        ("Draw ms", lambda r: r.get("draw_ms")),
        ("Awake ms", lambda r: r.get("awake_ms")),
        ("Render ms", lambda r: r.get("render_ms")),
        ("Next s", lambda r: r.get("next_wake_seconds")),
        ("FW", lambda r: r.get("firmware_version")),
    ]
    head = "".join(f"<th>{html.escape(name)}</th>" for name, _ in columns)
    body_rows = []
    for r in records:
        cells = "".join(cell(getter(r)) for _, getter in columns)
        body_rows.append(f"<tr>{cells}</tr>")
    body = "\n".join(body_rows) or (
        f'<tr><td colspan="{len(columns)}" class="muted">No telemetry yet.</td></tr>'
    )

    def num(v: object) -> str:
        return f"{v:.0f}" if isinstance(v, (int, float)) else "?"

    drain = summary.get("battery_drain_mv")
    summary_html = (
        f"Wakes: {summary['samples']} (total stored {summary['total_samples']}) &middot; "
        f"Actions: {summary['draws']} draw / {summary['noops']} noop / "
        f"{summary['sleeps']} sleep &middot; "
        f"Battery {num(summary['battery_min'])}-{num(summary['battery_max'])}% &middot; "
        f"Drain ~{num(drain)} mV/cycle &middot; "
        f"Draw cycles {summary.get('draw_cycles', 0)} (awake "
        f"{num(summary.get('draw_awake_avg'))} / draw {num(summary.get('draw_ms_avg'))} ms) "
        f"&middot; Idle cycles {summary.get('idle_cycles', 0)} (awake "
        f"{num(summary.get('idle_awake_avg'))} ms)"
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(settings.device_id)} telemetry</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 1.2rem; color: #1a1a1a; }}
  h1 {{ font-size: 1.2rem; margin: 0 0 .3rem; }}
  .summary {{ color: #333; font-size: .85rem; line-height: 1.6; margin-bottom: 1rem; }}
  .nav {{ font-size: .85rem; margin-bottom: 1rem; }}
  .nav a {{ margin-right: .8rem; }}
  table {{ border-collapse: collapse; width: 100%; font-size: .8rem; }}
  th, td {{ padding: .3rem .5rem; text-align: right; border-bottom: 1px solid #eee; white-space: nowrap; }}
  th {{ position: sticky; top: 0; background: #fafafa; border-bottom: 2px solid #ddd; }}
  td:first-child, th:first-child, td:nth-child(2), td:nth-child(3), td:nth-child(4) {{ text-align: left; }}
  tr:hover td {{ background: #f6f9ff; }}
  .muted {{ color: #aaa; }}
</style>
</head>
<body>
<h1>{html.escape(settings.device_id)} &middot; last {days} day(s)</h1>
<div class="summary">{summary_html}</div>
<div class="nav">
  Range: <a href="/stats?days=1">1d</a><a href="/stats?days=7">7d</a>
  <a href="/stats?days=30">30d</a>
  &middot; <a href="/stats?days={days}&amp;format=json">JSON</a>
</div>
<table>
<thead><tr>{head}</tr></thead>
<tbody>
{body}
</tbody>
</table>
</body>
</html>"""


@router.post("/api/device/{device_id}/status", response_model=ActionResponse)
async def device_status(
    payload: StatusRequest,
    device_id: str = Depends(require_device),
    services: Services = Depends(device_services),
) -> ActionResponse:
    return await services.handle_status(payload)


@router.get("/api/device/{device_id}/image/{image_id}")
async def device_image(
    image_id: str,
    device_id: str = Depends(require_device),
    services: Services = Depends(device_services),
) -> FileResponse:
    rendered = await queue_service.get_rendered_image(
        services.db, device_id, image_id
    )
    if rendered is None:
        raise HTTPException(status_code=404, detail="Image not found")
    if not os.path.exists(rendered.path):
        raise HTTPException(status_code=404, detail="Image file missing")
    # E1004 frames are packed 4bpp .bin buffers; M5 renders are PNGs. Serve the
    # content-type by extension so each device gets the right bytes.
    ext = os.path.splitext(rendered.path)[1].lower()
    media_type = "image/png" if ext == ".png" else "application/octet-stream"
    return FileResponse(
        rendered.path,
        media_type=media_type,
        filename=f"{image_id}{ext or '.png'}",
    )
