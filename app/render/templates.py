"""Jinja2 helpers that turn content into HTML for the headless renderer."""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from pathlib import Path

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
