"""Normalize weather icons into Spectra-6 PNGs for the weather card.

A one-off, local preprocessing step (not run at request time). The source icons
in ``app/assets/weather_icons/`` are 512x512 RGBA line art: the sun/moon are
yellow, rain drops and bolts are cyan/yellow, and the clouds/fog are a very
light gray. On a white card that light gray would vanish after quantization, so
here the cloud is the *subject* and must become black ink.

Unlike ``normalize_logos.py`` (which keys off luminance and sends light grays to
white), this script keys the ink mask off the alpha channel: every sufficiently
opaque pixel is ink. Ink pixels then snap to the palette by saturation
(saturated -> nearest chromatic; gray -> black); everything else is white. The
output is flattened on white, opaque, with hard edges, so the render-time
quantizer only ever sees exact palette colors (no green fringing, no mid-grays).

Re-run if the source icons change:

    python scripts/normalize_weather_icons.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

# Single source of truth for the palette.
from app.render.image_ops import SPECTRA6_PALETTE  # noqa: E402

SRC_DIR = _ROOT / "app" / "assets" / "weather_icons"
OUT_DIR = SRC_DIR / "normalized"

WHITE = (255, 255, 255)
BLACK = (0, 0, 0)

# Chromatic palette entries (no black/white) used for saturated pixels.
CHROMATIC = [c for c in SPECTRA6_PALETTE if c not in (BLACK, WHITE)]

#: Downscale target (longest side). Matches the card's hero icon size so the
#: bake happens at display resolution and is shown 1:1 (crisp, no resampling).
TARGET_PX = 128
#: A pixel counts as ink when its alpha is at least this.
ALPHA_CUTOFF = 110
#: Below this max-min chroma, an ink pixel is treated as gray (-> black).
SAT_THRESHOLD = 45


def _nearest_chromatic(r: int, g: int, b: int) -> tuple[int, int, int]:
    return min(
        CHROMATIC,
        key=lambda c: (c[0] - r) ** 2 + (c[1] - g) ** 2 + (c[2] - b) ** 2,
    )


def _normalize(img: Image.Image) -> Image.Image:
    """Alpha-masked snap to Spectra-6, flattened on white (opaque)."""
    rgba = img.convert("RGBA")
    rgba.thumbnail((TARGET_PX, TARGET_PX), Image.LANCZOS)
    src = rgba.load()
    w, h = rgba.size

    out = Image.new("RGB", (w, h), WHITE)
    dst = out.load()
    for y in range(h):
        for x in range(w):
            r, g, b, a = src[x, y]
            if a < ALPHA_CUTOFF:
                continue  # transparent -> white background
            if max(r, g, b) - min(r, g, b) < SAT_THRESHOLD:
                dst[x, y] = BLACK  # gray line art (clouds, fog) -> ink
            else:
                dst[x, y] = _nearest_chromatic(r, g, b)
    return out


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    sources = sorted(SRC_DIR.glob("*.png"))
    if not sources:
        print(f"no source icons found in {SRC_DIR}")
        return
    for src in sources:
        normalized = _normalize(Image.open(src))
        out = OUT_DIR / src.name
        normalized.save(out, format="PNG", optimize=True)
        print(f"{src.name} -> normalized/{out.name}  {normalized.size}")


if __name__ == "__main__":
    main()
