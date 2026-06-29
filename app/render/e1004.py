"""reTerminal E1004 (13.3" Spectra-6, T133A01 dual-chip) frame packer.

Server-side pipeline that turns an arbitrary image into the exact 4-bit-per-
pixel, GxEPD2-encoded full-frame buffer the device pushes straight into the
panel framebuffer via ``GxEPD2_T133A01_1200x1600::writeNative()``. The device
does NO dithering and NO image decoding -- it just copies these bytes into
PSRAM and triggers a refresh. All the (gamma-aware, error-diffusion) work
happens here, exactly like the M5 path in :mod:`app.render.image_ops`, just
emitting a packed nibble buffer instead of a PNG.

Wire format (matches the vendored driver's ``_frame_buf`` layout 1:1):
  * 1200 x 1600, row-major, 2 pixels per byte -> 600 bytes/row, 960000 total.
  * High nibble = even x (left pixel), low nibble = odd x (right pixel).
  * Each nibble is a GxEPD2 ``color7`` index, which the driver's
    ``_convert_to_native()`` maps to the panel's native ink code:
        0=Black 1=White 2=Green 3=Blue 4=Red 5=Yellow
    (Spectra 6 has no orange; index 6 is unused here.)
"""

from __future__ import annotations

import io

import epaper_dithering as epd
import numpy as np
from PIL import Image, ImageOps

from .image_ops import _flatten_alpha, auto_orient

E1004_WIDTH = 1200
E1004_HEIGHT = 1600
E1004_SIZE = (E1004_WIDTH, E1004_HEIGHT)

#: Size of the packed full-frame buffer the device expects (960000 bytes).
FRAME_BYTES = E1004_WIDTH * E1004_HEIGHT // 2

# Representative Spectra-6 RGB values to dither against. These are the same
# reference primaries used by the M5 ED2208 panel (both are E Ink Spectra 6),
# paired with the GxEPD2 color7 index that the T133A01 driver expects in its
# 4bpp framebuffer. TODO: photograph the actual E1004 panel and calibrate these
# RGB values for the best perceptual match (the GxEPD2 index column stays put).
#   name -> (RGB, GxEPD2 color7 index)
_PALETTE: dict[str, tuple[tuple[int, int, int], int]] = {
    "black": ((0, 0, 0), 0),
    "white": ((255, 255, 255), 1),
    "green": ((67, 138, 28), 2),
    "blue": ((100, 64, 255), 3),
    "red": ((191, 0, 0), 4),
    "yellow": ((255, 243, 56), 5),
}

_COLOR_PALETTE = epd.ColorPalette(
    colors={name: rgb for name, (rgb, _idx) in _PALETTE.items()},
    accent="red",
    scheme=epd.ColorScheme.BWGBRY,
)


def _fit_cover(
    img: Image.Image, background: tuple[int, int, int] = (255, 255, 255)
) -> Image.Image:
    """Center-crop/scale to exactly 1200x1600, flattening any alpha first."""
    img = _flatten_alpha(img, background)
    img = img.convert("RGB")
    return ImageOps.fit(img, E1004_SIZE, method=Image.LANCZOS)


def _gxepd2_lut(p_img: Image.Image) -> np.ndarray:
    """Map each P-mode palette index in ``p_img`` to a GxEPD2 color7 nibble.

    The dithered image only ever uses our six palette colors, so we match each
    palette entry by its exact RGB value. Anything unexpected falls back to
    white (1), which is the panel's safe "no ink" state.
    """
    palette = p_img.getpalette() or []
    rgb_to_idx = {rgb: idx for _name, (rgb, idx) in _PALETTE.items()}
    lut = np.ones(256, dtype=np.uint8)  # default -> white
    for i in range(min(256, len(palette) // 3)):
        rgb = (palette[i * 3], palette[i * 3 + 1], palette[i * 3 + 2])
        if rgb in rgb_to_idx:
            lut[i] = rgb_to_idx[rgb]
    return lut


def pack_p_image(p_img: Image.Image) -> bytes:
    """Pack a dithered P-mode 1200x1600 image into the device's 4bpp frame."""
    if p_img.mode != "P":
        raise ValueError(f"expected a P-mode image, got {p_img.mode!r}")
    if p_img.size != E1004_SIZE:
        raise ValueError(f"expected {E1004_SIZE}, got {p_img.size}")

    indices = np.asarray(p_img, dtype=np.uint8)  # (H, W) palette indices
    nibbles = _gxepd2_lut(p_img)[indices]  # (H, W) GxEPD2 color7 codes 0..5

    hi = nibbles[:, 0::2]  # even x -> high nibble
    lo = nibbles[:, 1::2]  # odd  x -> low nibble
    packed = ((hi << 4) | lo).astype(np.uint8)  # (H, W/2)
    data = packed.tobytes()
    assert len(data) == FRAME_BYTES, f"{len(data)} != {FRAME_BYTES}"
    return data


def render_e1004_frame(
    data: bytes,
    *,
    background: tuple[int, int, int] = (255, 255, 255),
    auto_rotate: bool = True,
    mode: epd.DitherMode = epd.DitherMode.FLOYD_STEINBERG,
) -> bytes:
    """Full pipeline: raw image bytes -> 960000-byte E1004 frame buffer.

    Runs one gamma-aware (linear-light + OKLab) error-diffusion pass to the
    Spectra-6 palette, then packs to the driver's GxEPD2 4bpp nibble layout.
    """
    img = Image.open(io.BytesIO(data))
    img = auto_orient(img, auto_rotate=auto_rotate)  # E1004 is portrait, like M5
    fitted = _fit_cover(img, background)
    dithered = epd.dither_image(
        fitted, _COLOR_PALETTE, mode=mode, serpentine=True
    )
    return pack_p_image(dithered)
