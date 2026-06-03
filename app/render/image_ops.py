"""Image processing: fit to 400x600 and convert to the Spectra-6 palette.

This module is intentionally free of any browser/Playwright dependency so it
can be unit-tested and reused for both rendered HTML and uploaded photos.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Literal

from PIL import Image, ImageOps

TARGET_WIDTH = 400
TARGET_HEIGHT = 600
TARGET_SIZE = (TARGET_WIDTH, TARGET_HEIGHT)

# Spectra 6 style limited color palette: black, white, red, yellow, blue, green.
SPECTRA6_PALETTE: list[tuple[int, int, int]] = [
    (0, 0, 0),        # black
    (255, 255, 255),  # white
    (220, 30, 30),    # red
    (240, 200, 40),   # yellow
    (40, 80, 200),    # blue
    (40, 160, 80),    # green
]

FitMode = Literal["cover", "contain"]


def _palette_image() -> Image.Image:
    """Build a P-mode image whose palette is the Spectra-6 palette."""
    pal_img = Image.new("P", (1, 1))
    flat: list[int] = []
    for rgb in SPECTRA6_PALETTE:
        flat.extend(rgb)
    # Pillow palettes hold 256 entries; pad the remainder with the last color.
    flat.extend(SPECTRA6_PALETTE[-1] * (256 - len(SPECTRA6_PALETTE)))
    pal_img.putpalette(flat)
    return pal_img


def fit_to_target(
    img: Image.Image,
    mode: FitMode = "cover",
    background: tuple[int, int, int] = (255, 255, 255),
) -> Image.Image:
    """Resize/crop/pad an image to exactly 400x600 preserving aspect ratio.

    ``cover``   -> center-crop to fill the whole display (no padding).
    ``contain`` -> fit inside and pad with ``background`` color.
    """
    img = img.convert("RGB")
    if mode == "cover":
        return ImageOps.fit(img, TARGET_SIZE, method=Image.LANCZOS)

    fitted = ImageOps.contain(img, TARGET_SIZE, method=Image.LANCZOS)
    canvas = Image.new("RGB", TARGET_SIZE, background)
    offset = (
        (TARGET_WIDTH - fitted.width) // 2,
        (TARGET_HEIGHT - fitted.height) // 2,
    )
    canvas.paste(fitted, offset)
    return canvas


def quantize_to_palette(img: Image.Image, dither: bool = True) -> Image.Image:
    """Map an RGB image onto the Spectra-6 palette with optional dithering."""
    rgb = img.convert("RGB")
    dither_mode = Image.Dither.FLOYDSTEINBERG if dither else Image.Dither.NONE
    return rgb.quantize(palette=_palette_image(), dither=dither_mode)


def png_bytes_to_display_png(
    data: bytes,
    fit_mode: FitMode = "cover",
    background: tuple[int, int, int] = (255, 255, 255),
    dither: bool = True,
) -> bytes:
    """Full pipeline: raw image bytes -> 400x600 Spectra-6 PNG bytes."""
    img = Image.open(io.BytesIO(data))
    fitted = fit_to_target(img, mode=fit_mode, background=background)
    quantized = quantize_to_palette(fitted, dither=dither)
    out = io.BytesIO()
    quantized.save(out, format="PNG", optimize=True)
    return out.getvalue()


def save_display_png(data: bytes, path: Path | str, **kwargs) -> None:
    """Process ``data`` and write the resulting display PNG to ``path``."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(png_bytes_to_display_png(data, **kwargs))


def make_blank_png(background: tuple[int, int, int] = (255, 255, 255)) -> bytes:
    """Produce a blank 400x600 PNG in the display palette."""
    img = Image.new("RGB", TARGET_SIZE, background)
    out = io.BytesIO()
    quantize_to_palette(img, dither=False).save(out, format="PNG", optimize=True)
    return out.getvalue()
