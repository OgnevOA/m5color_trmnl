"""Jinja2 helpers that turn content into HTML for the headless renderer."""

from __future__ import annotations

import base64
import io
import math
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import qrcode
from qrcode.constants import ERROR_CORRECT_M
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import weather_icons

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_ASSETS_DIR = Path(__file__).parent.parent / "assets"


@lru_cache(maxsize=None)
def _logo_data_uri(filename: str) -> tuple[str, int, int] | None:
    """Return ``(data_uri, width, height)`` for a normalized header PNG.

    Dimensions are parsed straight from the PNG IHDR chunk so the template can
    place the logo at its natural 1:1 pixel size (crisp, no browser scaling).
    """
    path = _ASSETS_DIR / filename
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:image/png;base64,{encoded}", width, height


@lru_cache
def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )


@lru_cache
def _base_css() -> str:
    return (_TEMPLATES_DIR / "base.css").read_text(encoding="utf-8")


@lru_cache
def _weather_css() -> str:
    return (_TEMPLATES_DIR / "weather.css").read_text(encoding="utf-8")


@lru_cache
def _overlay_css() -> str:
    return (_TEMPLATES_DIR / "overlay.css").read_text(encoding="utf-8")


@lru_cache
def _collage_css() -> str:
    return (_TEMPLATES_DIR / "collage.css").read_text(encoding="utf-8")


# Mosaic presets keyed by tile count. Each cell is ``(col, cspan, row, rspan)``
# on a ``cols x rows`` grid; the cells tile the grid without gaps/overlaps. The
# count=3 preset is an internal fallback used when fewer works than requested
# resolve. Layouts are portrait-friendly (one big "featured" cell + a mix of
# tall/wide/square cells) so the result reads like a curated wall.
_COLLAGE_PRESETS: dict[int, dict] = {
    3: {
        "cols": 2,
        "rows": 2,
        "cells": [(1, 1, 1, 2), (2, 1, 1, 1), (2, 1, 2, 1)],
    },
    4: {
        "cols": 2,
        "rows": 3,
        "cells": [(1, 2, 1, 1), (1, 1, 2, 2), (2, 1, 2, 1), (2, 1, 3, 1)],
    },
    6: {
        "cols": 3,
        "rows": 4,
        "cells": [
            (1, 2, 1, 2),
            (3, 1, 1, 2),
            (1, 1, 3, 2),
            (2, 1, 3, 1),
            (3, 1, 3, 1),
            (2, 2, 4, 1),
        ],
    },
    9: {
        "cols": 3,
        "rows": 5,
        "cells": [
            (1, 2, 1, 2),
            (3, 1, 1, 1),
            (3, 1, 2, 1),
            (1, 1, 3, 1),
            (2, 1, 3, 1),
            (3, 1, 3, 2),
            (1, 2, 4, 1),
            (1, 1, 5, 1),
            (2, 2, 5, 1),
        ],
    },
}

#: Small fixed set of "zoom" (extra crop) levels + offsets, so accent tiles
#: bleed a little differently without looking random. Indexed deterministically.
_COLLAGE_ZOOMS = (1.0, 1.14, 1.22, 1.1)
_COLLAGE_OFFSETS = ("0%", "-4%", "4%", "-3%", "3%")


def _collage_preset(n_tiles: int) -> dict:
    """Largest preset whose cell count is <= ``n_tiles`` (min 3)."""
    usable = [c for c in sorted(_COLLAGE_PRESETS) if c <= n_tiles]
    return _COLLAGE_PRESETS[usable[-1] if usable else 3]


def _cell_kind(cspan: int, rspan: int) -> str:
    if cspan > rspan:
        return "wide"
    if rspan > cspan:
        return "tall"
    return "square"


def _assign_collage_tiles(cells: list[tuple], tiles: list[dict]) -> list[dict]:
    """Match works to cells by orientation (landscape->wide, portrait->tall).

    Bigger cells are filled first so the featured/wide cells get the best-fitting
    works; each work is used once.
    """
    def aspect(t: dict) -> float:
        w, h = t.get("width") or 1, t.get("height") or 1
        return (w / h) if h else 1.0

    remaining = list(tiles)
    # Fill larger cells first (better orientation match on the prominent tiles).
    order = sorted(
        range(len(cells)), key=lambda i: cells[i][1] * cells[i][3], reverse=True
    )
    featured_idx = order[0] if order else None

    chosen: dict[int, dict] = {}
    for idx in order:
        if not remaining:
            break
        _, cspan, _, rspan = cells[idx]
        kind = _cell_kind(cspan, rspan)
        if kind == "wide":
            pick = max(remaining, key=aspect)
        elif kind == "tall":
            pick = min(remaining, key=aspect)
        else:  # square: closest to 1:1
            pick = min(remaining, key=lambda t: abs(aspect(t) - 1.0))
        remaining.remove(pick)
        chosen[idx] = pick

    out: list[dict] = []
    for idx, (col, cspan, row, rspan) in enumerate(cells):
        tile = chosen.get(idx)
        if tile is None:
            continue
        featured = idx == featured_idx
        # Deterministic per-title crop so refills of the same set look stable.
        seed = sum(ord(ch) for ch in str(tile.get("title") or idx))
        zoom = _COLLAGE_ZOOMS[seed % len(_COLLAGE_ZOOMS)]
        if featured:
            zoom = max(zoom, 1.08)
        dx = _COLLAGE_OFFSETS[seed % len(_COLLAGE_OFFSETS)] if zoom > 1.01 else "0%"
        dy = _COLLAGE_OFFSETS[(seed // 3) % len(_COLLAGE_OFFSETS)] if zoom > 1.01 else "0%"
        out.append(
            {
                "col": col,
                "cspan": cspan,
                "row": row,
                "rspan": rspan,
                "featured": featured,
                "uri": tile["uri"],
                "title": tile.get("title"),
                "year": tile.get("year"),
                "zoom": round(zoom, 3),
                "dx": dx,
                "dy": dy,
            }
        )
    return out


def render_collage_html(artist_label: str, tiles: list[dict]) -> str:
    """Render a mosaic of several works by one artist.

    ``tiles`` is ``[{uri, title, year, width, height}, ...]`` (a data URI plus
    metadata per work). The layout preset is chosen from the number of tiles and
    each work is placed by orientation; extra tiles beyond the preset are unused.
    """
    preset = _collage_preset(len(tiles))
    cells = _assign_collage_tiles(preset["cells"], tiles)
    template = _env().get_template("collage.html")
    return template.render(
        collage_css=_collage_css(),
        artist_label=artist_label,
        cols=preset["cols"],
        rows=preset["rows"],
        cells=cells,
    )


def render_overlay_html(bg_uri: str, **context) -> str:
    """Render the artwork info overlay (background + calendar/caption/weather).

    ``context`` is the dict built by :func:`app.render.overlay.build_context`
    (calendar matrix, date, caption, weather summary, per-block themes and the
    band/column percentages).
    """
    template = _env().get_template("overlay.html")
    return template.render(overlay_css=_overlay_css(), bg_uri=bg_uri, **context)


def render_text_html(
    body: str,
    title: str = "Message",
    footer_left: str = "m5color-trmnl",
    footer_right: str | None = None,
) -> str:
    """Render plain text content into a styled HTML page."""
    template = _env().get_template("text.html")
    return template.render(
        base_css=_base_css(),
        title=title,
        body=body,
        footer_left=footer_left,
        footer_right=footer_right or datetime.now().strftime("%Y-%m-%d %H:%M"),
    )


def render_placeholder_html(title: str, body: str) -> str:
    """Render a placeholder page (e.g. for unknown modes)."""
    template = _env().get_template("placeholder.html")
    return template.render(
        base_css=_base_css(),
        title=title,
        body=body,
        footer_right=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )


def _font_size_for(text_length: int) -> int:
    """Pick a font size for a multi-line dialogue so it fits the 400x600 panel."""
    if text_length <= 80:
        return 28
    if text_length <= 160:
        return 23
    if text_length <= 260:
        return 19
    if text_length <= 380:
        return 16
    return 14


def _single_quote_font_size(text_length: int) -> int:
    """Larger sizes for a single centered quote (serif, italic)."""
    if text_length <= 60:
        return 34
    if text_length <= 120:
        return 28
    if text_length <= 200:
        return 23
    if text_length <= 320:
        return 19
    return 16


def _qr_png_data_uri(data: str, target_px: int = 340) -> tuple[str, int]:
    """Build a crisp black/white QR PNG and return ``(data_uri, size_px)``.

    The QR is rendered at an integer pixels-per-module size so it stays sharp
    after the Spectra-6 quantization (no gray edges that would dither into
    noise). ``size_px`` is the natural pixel size; the template displays the
    image 1:1 with ``image-rendering: pixelated`` to avoid any blur.
    """
    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, border=3)
    qr.add_data(data)
    qr.make(fit=True)
    total_modules = qr.modules_count + 2 * qr.border
    qr.box_size = max(1, target_px // total_modules)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}", img.size[0]


def render_qr_html(data: str, caption: str | None = None) -> str:
    """Render a QR code page encoding ``data`` (text or URL)."""
    uri, size_px = _qr_png_data_uri(data)
    shown = caption if caption is not None else data
    if len(shown) > 140:
        shown = shown[:139] + "\u2026"
    template = _env().get_template("qr.html")
    return template.render(
        base_css=_base_css(),
        qr_uri=uri,
        qr_size=size_px,
        caption=shown,
        footer_right=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )


def render_quote_card_html(
    show_title: str,
    dialogue: list[dict],
    season: int | None = None,
    episode: int | None = None,
    episode_title: str | None = None,
    logo_file: str | None = None,
) -> str:
    """Render a TV-show quote card (serif italic quote, black on white).

    ``dialogue`` is a list of ``{"speaker": str, "text": str}`` items. A single
    line renders as one big centered quote with the speaker in the footer; a
    multi-line exchange renders each line with an inline speaker label.

    ``logo_file`` is a normalized header PNG in ``app/assets``; when present it
    is shown as the card header, otherwise ``show_title`` is used as text.
    """
    single = len(dialogue) == 1
    total = sum(len(str(d.get("text", ""))) for d in dialogue)
    speaker = str(dialogue[0].get("speaker", "")).strip() if single else ""
    text = str(dialogue[0].get("text", "")).strip() if single else ""

    if season and episode:
        ep_code = f"S{season} \u00b7 E{episode}"
    elif season:
        ep_code = f"Season {season}"
    else:
        ep_code = ""

    logo = _logo_data_uri(logo_file) if logo_file else None
    logo_uri, logo_w, logo_h = logo if logo else (None, 0, 0)

    font_px = _single_quote_font_size(total) if single else _font_size_for(total)
    template = _env().get_template("quote.html")
    return template.render(
        base_css=_base_css(),
        show_title=show_title,
        dialogue=dialogue,
        single=single,
        speaker=speaker,
        text=text,
        ep_code=ep_code,
        episode_title=episode_title or "",
        logo_uri=logo_uri,
        logo_w=logo_w,
        logo_h=logo_h,
        font_px=font_px,
    )


# Sunrise/sunset arc geometry (flat SVG, palette colors only).
_ARC_W, _ARC_H = 304, 116
_ARC_CX, _ARC_BASE_Y, _ARC_R = 152, 104, 92
_SUN_YELLOW = "#f6da48"


def _sun_arc_svg(daylight_frac: float) -> str:
    """A flat semicircle with a sun marker placed by ``daylight_frac`` (0..1).

    0 sits at the left foot (sunrise), 1 at the right foot (sunset), 0.5 at the
    apex. Only black strokes and a yellow sun fill are used so it quantizes
    cleanly on the Spectra-6 panel.
    """
    frac = max(0.0, min(1.0, daylight_frac))
    angle = math.pi * (1.0 - frac)  # 180deg (sunrise) -> 0deg (sunset)
    sun_x = _ARC_CX + _ARC_R * math.cos(angle)
    sun_y = _ARC_BASE_Y - _ARC_R * math.sin(angle)
    left_x, right_x = _ARC_CX - _ARC_R, _ARC_CX + _ARC_R
    return (
        f'<svg viewBox="0 0 {_ARC_W} {_ARC_H}" width="{_ARC_W}" height="{_ARC_H}" '
        f'xmlns="http://www.w3.org/2000/svg">'
        f'<path d="M {left_x} {_ARC_BASE_Y} A {_ARC_R} {_ARC_R} 0 0 1 '
        f'{right_x} {_ARC_BASE_Y}" fill="none" stroke="#000000" '
        f'stroke-width="2" stroke-dasharray="2 7" stroke-linecap="round"/>'
        f'<line x1="{left_x}" y1="{_ARC_BASE_Y}" x2="{right_x}" y2="{_ARC_BASE_Y}" '
        f'stroke="#000000" stroke-width="2"/>'
        f'<circle cx="{left_x}" cy="{_ARC_BASE_Y}" r="3.5" fill="#000000"/>'
        f'<circle cx="{right_x}" cy="{_ARC_BASE_Y}" r="3.5" fill="#000000"/>'
        f'<circle cx="{sun_x:.1f}" cy="{sun_y:.1f}" r="13" fill="{_SUN_YELLOW}" '
        f'stroke="#000000" stroke-width="2.5"/>'
        f"</svg>"
    )


def render_weather_html(data: dict) -> str:
    """Render the weather card from a display dict built by ``WeatherMode``."""
    code = data.get("icon", "01d")
    icon = weather_icons.icon_data_uri(code)
    icon_uri, icon_w, icon_h = icon if icon else (None, 0, 0)
    accent = weather_icons.accent_for(code)

    lo, hi, temp = data.get("lo"), data.get("hi"), data.get("temp")
    if lo is None or hi is None or hi == lo:
        now_frac = 0.5
    else:
        now_frac = max(0.0, min(1.0, (temp - lo) / (hi - lo)))

    template = _env().get_template("weather.html")
    return template.render(
        base_css=_base_css(),
        weather_css=_weather_css(),
        accent=accent,
        icon_uri=icon_uri,
        icon_w=icon_w,
        icon_h=icon_h,
        arc_svg=_sun_arc_svg(data.get("daylight_frac", 0.5)),
        now_frac_pct=round(now_frac * 100, 1),
        footer_right=datetime.now().strftime("%Y-%m-%d %H:%M"),
        **data,
    )
