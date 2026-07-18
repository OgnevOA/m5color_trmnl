"""Face detection for face-aware collage cropping (OpenCV YuNet).

Used only by the user-photo collage: each tile's crop focal point is derived
from where the faces are, so a face is never cut off by the cover-crop. This is
best-effort -- if OpenCV or the model is unavailable, detection returns no faces
and the caller falls back to a plain center-crop, so the feature never breaks a
render.

The heavy work (model inference) is synchronous CPU; callers should invoke
:func:`detect_faces_normalized` via ``asyncio.to_thread`` to keep the event loop
responsive.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

_MODEL_PATH = Path(__file__).parent.parent / "assets" / "models" / (
    "face_detection_yunet_2023mar.onnx"
)
#: Longest side (px) the image is downscaled to before detection. YuNet is fast
#: and the boxes are returned normalized, so a smaller working image keeps
#: latency low without hurting focal-point accuracy.
_DETECT_LONG_SIDE = 640
#: YuNet score threshold; a bit strict to avoid spurious "faces" pulling the
#: crop toward background clutter.
_SCORE_THRESHOLD = 0.7

_detector = None  # cv2.FaceDetectorYN, created lazily
_detector_input = (0, 0)  # last (w, h) the detector input size was set to
_lock = threading.Lock()
_load_failed = False


def _get_detector():
    """Return a cached ``cv2.FaceDetectorYN`` or ``None`` if unavailable."""
    global _detector, _load_failed
    if _detector is not None:
        return _detector
    if _load_failed:
        return None
    with _lock:
        if _detector is not None:
            return _detector
        if _load_failed:
            return None
        try:
            import cv2  # imported lazily so a missing/broken OpenCV degrades

            if not _MODEL_PATH.exists():
                raise FileNotFoundError(f"YuNet model missing: {_MODEL_PATH}")
            _detector = cv2.FaceDetectorYN_create(
                str(_MODEL_PATH),
                "",
                (320, 320),  # placeholder; reset per-image via setInputSize
                score_threshold=_SCORE_THRESHOLD,
            )
        except Exception as exc:  # OpenCV missing, model unreadable, etc.
            _load_failed = True
            logger.warning("face detection unavailable (%s); using center crops", exc)
            return None
    return _detector


def detect_faces_normalized(
    img: Image.Image,
) -> list[tuple[float, float, float, float]]:
    """Detect faces and return boxes as normalized ``(x, y, w, h)`` in [0, 1].

    ``x, y`` is the top-left corner. Returns an empty list when detection is
    unavailable or no face is found (caller then center-crops).
    """
    detector = _get_detector()
    if detector is None:
        return []
    try:
        import cv2

        rgb = img.convert("RGB")
        w0, h0 = rgb.size
        if w0 == 0 or h0 == 0:
            return []
        scale = min(1.0, _DETECT_LONG_SIDE / max(w0, h0))
        w = max(1, int(round(w0 * scale)))
        h = max(1, int(round(h0 * scale)))
        if (w, h) != rgb.size:
            rgb = rgb.resize((w, h), Image.BILINEAR)
        # OpenCV wants BGR uint8.
        arr = np.asarray(rgb, dtype=np.uint8)[:, :, ::-1]
        with _lock:  # FaceDetectorYN is stateful (input size); serialize use
            detector.setInputSize((w, h))
            _, faces = detector.detect(arr)
        if faces is None:
            return []
        out: list[tuple[float, float, float, float]] = []
        for f in faces:
            fx, fy, fw, fh = float(f[0]), float(f[1]), float(f[2]), float(f[3])
            # Clamp to the frame and normalize.
            nx = min(max(fx / w, 0.0), 1.0)
            ny = min(max(fy / h, 0.0), 1.0)
            nw = min(max(fw / w, 0.0), 1.0 - nx)
            nh = min(max(fh / h, 0.0), 1.0 - ny)
            if nw > 0 and nh > 0:
                out.append((nx, ny, nw, nh))
        return out
    except Exception as exc:  # never let detection break a render
        logger.debug("face detection failed: %s", exc)
        return []


def union_box(
    faces: list[tuple[float, float, float, float]],
    pad: float = 0.08,
) -> Optional[tuple[float, float, float, float]]:
    """Padded bounding box (normalized) enclosing all ``faces``, or ``None``.

    ``pad`` grows the box by a fraction of its size (clamped to the frame) so a
    little headroom/chin/hair is kept around the faces, not just the eyes-nose
    rectangle YuNet returns.
    """
    if not faces:
        return None
    x0 = min(f[0] for f in faces)
    y0 = min(f[1] for f in faces)
    x1 = max(f[0] + f[2] for f in faces)
    y1 = max(f[1] + f[3] for f in faces)
    bw, bh = x1 - x0, y1 - y0
    x0 = max(0.0, x0 - bw * pad)
    y0 = max(0.0, y0 - bh * pad)
    x1 = min(1.0, x1 + bw * pad)
    y1 = min(1.0, y1 + bh * pad)
    return (x0, y0, x1 - x0, y1 - y0)
