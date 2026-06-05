"""Normalize TV-show logos into Spectra-6 header images for the quote cards.

This is a one-off, local preprocessing step (not run at request time). It takes
the raw logos in ``app/assets/`` and produces clean, palette-quantized header
PNGs (black-on-white, only Spectra-6 colors) that are embedded into the quote
card template. Re-run it if the source logos change:

    python scripts/normalize_logos.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageOps

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

# Single source of truth for the palette.
from app.render.image_ops import SPECTRA6_PALETTE  # noqa: E402

ASSETS = _ROOT / "app" / "assets"

WHITE = (255, 255, 255)
MAX_W, MAX_H = 300, 52

# Chromatic palette entries (no black/white) used for saturated pixels.
CHROMATIC = [c for c in SPECTRA6_PALETTE if c not in ((0, 0, 0), WHITE)]
SAT_THRESHOLD = 55   # below this, a pixel is treated as gray
LUMA_THRESHOLD = 150  # gray pixels darker than this become black, else white

CONFIGS = [
    {"src": "Friends_logo.svg.png", "out": "friends_header.png"},
    {
        "src": "Scrubs_(TV_series)_logo.svg.png",
        "out": "scrubs_header.png",
        "tint": SPECTRA6_PALETTE[5],  # recolor teal -> palette green via alpha mask
    },
    {
        "src": "The Office TV Show Sign Logo Vector.svg .png",
        "out": "office_header.png",
        "invert": True,
    },
]


def _flatten_on_white(img: Image.Image) -> Image.Image:
    rgba = img.convert("RGBA")
    bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    return Image.alpha_composite(bg, rgba).convert("RGB")


def _tint_from_alpha(img: Image.Image, color: tuple[int, int, int]) -> Image.Image:
    """Paint ``color`` where the source is opaque, white elsewhere."""
    rgba = img.convert("RGBA")
    alpha = rgba.split()[-1]
    solid = Image.new("RGB", rgba.size, color)
    base = Image.new("RGB", rgba.size, WHITE)
    return Image.composite(solid, base, alpha)


def _trim_white(img: Image.Image) -> Image.Image:
    diff = ImageChops.difference(img, Image.new("RGB", img.size, WHITE))
    bbox = diff.getbbox()
    return img.crop(bbox) if bbox else img


def _snap_to_palette(img: Image.Image) -> Image.Image:
    """Map to Spectra-6, but resolve gray (anti-aliased) pixels to black/white.

    A plain palette quantizer maps mid-gray to green (nearest numerically),
    which fringes anti-aliased edges. Here, low-saturation pixels snap to
    black/white by luminance; only saturated pixels pick a chromatic color.
    """
    img = img.convert("RGB")
    px = img.load()
    w, h = img.size
    for y in range(h):
        for x in range(w):
            r, g, b = px[x, y]
            if max(r, g, b) - min(r, g, b) < SAT_THRESHOLD:
                lum = (r * 299 + g * 587 + b * 114) // 1000
                px[x, y] = (0, 0, 0) if lum < LUMA_THRESHOLD else WHITE
            else:
                px[x, y] = min(
                    CHROMATIC,
                    key=lambda c: (c[0] - r) ** 2 + (c[1] - g) ** 2 + (c[2] - b) ** 2,
                )
    return img


def process(cfg: dict) -> None:
    src = ASSETS / cfg["src"]
    if cfg.get("tint"):
        img = _tint_from_alpha(Image.open(src), cfg["tint"])
    else:
        img = _flatten_on_white(Image.open(src))
        if cfg.get("invert"):
            img = ImageOps.invert(img)
    img = _trim_white(img)
    img.thumbnail((MAX_W, MAX_H), Image.LANCZOS)
    snapped = _snap_to_palette(img)
    out = ASSETS / cfg["out"]
    snapped.save(out, format="PNG", optimize=True)
    print(f"{cfg['src']} -> {cfg['out']}  {snapped.size}")


def main() -> None:
    for cfg in CONFIGS:
        process(cfg)


if __name__ == "__main__":
    main()
