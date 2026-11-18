"""OCR engine wrapper: preprocessing, single passes and digit-level voting.

Design notes
------------
* RapidOCR (PP-OCRv4, ONNX) runs fully offline on CPU - no API keys, no sudo.
* We never trust one pass. The tracking number is re-read from tight crops at
  several zoom levels and voted character-by-character, so we can report
  *which digits are uncertain* instead of guessing silently.
"""

from __future__ import annotations

import io
import re
from collections import Counter
from dataclasses import dataclass
from typing import Iterable

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

_ENGINE = None


def engine():
    """Lazily build the RapidOCR engine (model load is ~0.5s)."""
    global _ENGINE
    if _ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR

        _ENGINE = RapidOCR()
    return _ENGINE


@dataclass
class OcrLine:
    text: str
    box: tuple[float, float, float, float]  # x0, y0, x1, y1 in *original* pixels
    conf: float
    angle: float = 0.0                      # baseline angle in degrees

    @property
    def cx(self) -> float:
        return (self.box[0] + self.box[2]) / 2

    @property
    def cy(self) -> float:
        return (self.box[1] + self.box[3]) / 2

    @property
    def height(self) -> float:
        return max(1.0, self.box[3] - self.box[1])


# --------------------------------------------------------------------------- #
# image preparation
# --------------------------------------------------------------------------- #

def load_image(data: bytes, max_side: int = 2600) -> Image.Image:
    """EXIF-correct, bounded, RGB image from raw bytes."""
    im = Image.open(io.BytesIO(data))
    im = ImageOps.exif_transpose(im)
    if im.mode != "RGB":
        im = im.convert("RGB")
    w, h = im.size
    scale = min(1.0, max_side / max(w, h))
    if scale < 1.0:
        im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    return im


def prepare(im: Image.Image, target_width: int = 1800) -> tuple[Image.Image, float]:
    """Grayscale + contrast + gentle upscale for small/soft photos.

    Returns (image, scale) where scale maps the prepared image back to the
    original pixel space (original = prepared / scale is *not* used; we map
    boxes by dividing by scale).
    """
    g = im.convert("L")
    w, h = g.size
    scale = 1.0
    if w < target_width:
        scale = min(2.5, target_width / w)
        g = g.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    g = ImageOps.autocontrast(g, cutoff=1)
    g = ImageEnhance.Contrast(g).enhance(1.35)
    g = g.filter(ImageFilter.UnsharpMask(radius=1.6, percent=140, threshold=3))
    return g, scale


def _raw_pass(img: Image.Image, scale: float) -> list[OcrLine]:
    arr = np.array(img.convert("RGB"))
    result, _elapse = engine()(arr)
    out: list[OcrLine] = []
    if not result:
        return out
    for box, text, conf in result:
        xs = [p[0] / scale for p in box]
        ys = [p[1] / scale for p in box]
        # baseline from the top edge of the detection quad
        try:
            angle = float(np.degrees(np.arctan2(box[1][1] - box[0][1], box[1][0] - box[0][0])))
        except Exception:                                    # noqa: BLE001
            angle = 0.0
        text = (text or "").strip()
        if text:
            out.append(OcrLine(text, (min(xs), min(ys), max(xs), max(ys)), float(conf), angle))
    return out


def estimate_skew(im: Image.Image) -> float:
    """Page skew in degrees, from the document's foreground shape (OpenCV)."""
    try:
        import cv2

        gray = np.array(im.convert("L"))
        small = cv2.resize(gray, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
        thresh = cv2.threshold(small, 0, 255,
                               cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
        coords = np.column_stack(np.nonzero(thresh))
        if len(coords) < 500:
            return 0.0
        rect = cv2.minAreaRect(coords.astype(np.float32))
        angle = rect[-1]
        if angle > 45:
            angle -= 90
        elif angle < -45:
            angle += 90
        return float(angle)
    except Exception:                                        # noqa: BLE001
        return 0.0


def deskew(im: Image.Image, angle: float) -> Image.Image:
    return im.rotate(angle, resample=Image.BICUBIC, expand=True, fillcolor=(255, 255, 255))


def run_ocr(im: Image.Image) -> list[OcrLine]:
    """Main full-image pass."""
    prepared, scale = prepare(im)
    return _raw_pass(prepared, scale)


def reocr_region(im: Image.Image, box: tuple[float, float, float, float],
                 zoom: float, pad: int = 8) -> tuple[str, float]:
    """Re-read one region at extra zoom. Returns (text, confidence)."""
    w, h = im.size
    x0 = max(0, box[0] - pad)
    y0 = max(0, box[1] - pad)
    x1 = min(w, box[2] + pad)
    y1 = min(h, box[3] + pad)
    if x1 - x0 < 4 or y1 - y0 < 4:
        return "", 0.0
    crop = im.crop((int(x0), int(y0), int(x1), int(y1))).convert("L")
    crop = crop.resize((max(8, int(crop.width * zoom)), max(8, int(crop.height * zoom))),
                       Image.LANCZOS)
    crop = ImageEnhance.Contrast(crop).enhance(1.9)
    crop = crop.filter(ImageFilter.UnsharpMask(radius=1.2, percent=160, threshold=2))
    arr = np.array(crop.convert("RGB"))
    result, _ = engine()(arr)
    if not result:
        return "", 0.0
    # Join fragments left-to-right in case the crop splits.
    parts = sorted(result, key=lambda r: min(p[0] for p in r[0]))
    text = "".join((p[1] or "").replace(" ", "") for p in parts)
    conf = float(np.mean([p[2] for p in parts])) if parts else 0.0
    return text, conf


# --------------------------------------------------------------------------- #
# voting
# --------------------------------------------------------------------------- #

def vote(readings: Iterable[tuple[str, float]],
         patterns: Iterable[str] = ()) -> tuple[str, float, list[float]]:
    """Character-wise majority vote across readings of the same token.

    Returns (voted_text, agreement 0..1, per_character agreement list).
    Readings of differing length are grouped; the strongest group wins, so a
    mis-segmented read (extra/missing char) cannot corrupt the vote. When the
    courier's expected shapes are known, a group that matches one of them
    outranks a bigger group that does not.
    """
    import re as _re

    items = [(_re.sub(r"[^A-Za-z0-9]", "", t).upper(), c) for t, c in readings]
    items = [(t, c) for t, c in items if t]
    if not items:
        return "", 0.0, []

    groups: dict[int, list[tuple[str, float]]] = {}
    for t, c in items:
        groups.setdefault(len(t), []).append((t, c))

    compiled = [_re.compile(p) for p in patterns]

    def majority(g: list[tuple[str, float]]) -> tuple[str, list[float]]:
        n = len(g[0][0])
        chars, per_char = [], []
        for i in range(n):
            col = Counter(t[i] for t, _ in g)
            ch, count = col.most_common(1)[0]
            chars.append(ch)
            per_char.append(count / len(g))
        return "".join(chars), per_char

    scored = []
    for g in groups.values():
        text, per_char = majority(g)
        group_score = len(g) + sum(c for _, c in g) / max(1, len(g))
        # earlier patterns in the courier's list are the more specific shapes
        rank = -999
        for i, p in enumerate(compiled):
            if p.match(text):
                rank = -i
                break
        scored.append((rank, group_score, text, per_char))

    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    _r, _score, text, per_char = scored[0]
    return text, sum(per_char) / len(per_char), per_char
