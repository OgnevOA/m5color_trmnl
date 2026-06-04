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
) -> str:
    """Render a TV-show quote card (serif italic quote, black on white).

    ``dialogue`` is a list of ``{"speaker": str, "text": str}`` items. A single
    line renders as one big centered quote with the speaker in the footer; a
    multi-line exchange renders each line with an inline speaker label.
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
        font_px=font_px,
    )
