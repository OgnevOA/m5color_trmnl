"""Jinja2 helpers that turn content into HTML for the headless renderer."""

from __future__ import annotations

import base64
import io
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import qrcode
from qrcode.constants import ERROR_CORRECT_M
from jinja2 import Environment, FileSystemLoader, select_autoescape

_TEMPLATES_DIR = Path(__file__).parent / "templates"


@lru_cache
def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )


@lru_cache
def _base_css() -> str:
    return (_TEMPLATES_DIR / "base.css").read_text(encoding="utf-8")


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
    """Pick a font size so longer quotes still fit the 400x600 panel."""
    if text_length <= 80:
        return 30
    if text_length <= 160:
        return 24
    if text_length <= 260:
        return 20
    if text_length <= 380:
        return 17
    return 15


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


def render_friends_quote_html(
    dialogue: list[dict],
    attribution: str = "",
) -> str:
    """Render a Friends quote card.

    ``dialogue`` is a list of ``{"speaker": str, "text": str}`` items.
    """
    total = sum(len(str(d.get("text", ""))) for d in dialogue)
    template = _env().get_template("friends.html")
    return template.render(
        base_css=_base_css(),
        dialogue=dialogue,
        attribution=attribution,
        font_px=_font_size_for(total),
    )
