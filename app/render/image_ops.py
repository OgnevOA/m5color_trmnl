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

# Spectra 6 limited color palette: black, white, red, yellow, blue, green.
# Values are tuned a bit lighter/closer to the panel's actual ink colors so
# midtones map to lighter inks (the previous darker primaries made photos look
# muddy when neutral midtones snapped to dark green/blue).
SPECTRA6_PALETTE: list[tuple[int, int, int]] = [
    (0, 0, 0),        # black
    (255, 255, 255),  # white
    (228, 64, 60),    # red
    (246, 218, 72),   # yellow
    (66, 110, 214),   # blue
    (78, 180, 116),   # green
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


def auto_orient(img: Image.Image, auto_rotate: bool = True) -> Image.Image:
    """Normalize image orientation for the portrait 400x600 panel.

    1. Apply the EXIF orientation tag (phone photos are often stored rotated).
    2. If ``auto_rotate`` and the image orientation does not match the target
       (e.g. a landscape photo on a portrait display), rotate it 90 degrees so
       it fills the frame instead of being heavily cropped.
    """
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass

    if auto_rotate:
        target_is_landscape = TARGET_WIDTH > TARGET_HEIGHT
        img_is_landscape = img.width > img.height
        if img_is_landscape != target_is_landscape:
            # expand=True keeps the full image; rotate clockwise.
            img = img.rotate(-90, expand=True)
    return img


def _flatten_alpha(
    img: Image.Image, background: tuple[int, int, int] = (255, 255, 255)
) -> Image.Image:
    """Composite any transparency onto ``background`` and return an RGB image."""
    if img.mode in ("RGBA", "LA") or (
        img.mode == "P" and "transparency" in img.info
    ):
        rgba = img.convert("RGBA")
        canvas = Image.new("RGBA", rgba.size, (*background, 255))
        canvas.alpha_composite(rgba)
        return canvas.convert("RGB")
    return img


def fit_to_target(
    img: Image.Image,
    mode: FitMode = "cover",
    background: tuple[int, int, int] = (255, 255, 255),
) -> Image.Image:
    """Resize/crop/pad an image to exactly 400x600 preserving aspect ratio.

    ``cover``   -> center-crop to fill the whole display (no padding).
    ``contain`` -> fit inside and pad with ``background`` color.

    Images with alpha (transparent stickers/PNGs) are flattened onto
    ``background`` first; converting RGBA straight to RGB drops alpha onto black,
    which would render transparent areas as an ugly black box.
    """
    img = _flatten_alpha(img, background)
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
    auto_rotate: bool = True,
    quantize: bool = False,
) -> bytes:
    """Full pipeline: raw image bytes -> 400x600 PNG bytes for the device.

    By default this returns a smooth 24-bit RGB PNG and lets the panel do the
    single RGB->Spectra-6 conversion/dithering on-device (avoids the previous
    double-dithering: server Floyd-Steinberg + panel ordered dither). Pass
    ``quantize=True`` to fall back to a server-side palette PNG (``dither``
    then selects Floyd-Steinberg vs. nearest-color).
    """
    img = Image.open(io.BytesIO(data))
    img = auto_orient(img, auto_rotate=auto_rotate)
    fitted = fit_to_target(img, mode=fit_mode, background=background)
    out = io.BytesIO()
    if quantize:
        quantize_to_palette(fitted, dither=dither).save(
            out, format="PNG", optimize=True
        )
    else:
        fitted.convert("RGB").save(out, format="PNG", optimize=True)
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
