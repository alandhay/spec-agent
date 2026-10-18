"""Client-area geometry, cropping, scaling and image comparison helpers.

Everything except :func:`client_area` is pure Pillow and runs on any OS.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Sequence

from PIL import Image, ImageChops

from .errors import CaptureError

Box = tuple[int, int, int, int]

# Smallest plausible Spectrum display (paper only) when auto-detecting.
_MIN_NATIVE_W, _MIN_NATIVE_H = 256, 192
_MAX_SCALE = 8


def client_area(hwnd: int) -> dict[str, int]:
    """Return the window's client area in screen coordinates (mss monitor dict)."""
    import win32gui

    _, _, width, height = win32gui.GetClientRect(hwnd)
    left, top = win32gui.ClientToScreen(hwnd, (0, 0))
    return {"left": left, "top": top, "width": width, "height": height}


def apply_crop(img: Image.Image, crop: Sequence[int] | None) -> Image.Image:
    """Trim ``[left, top, right, bottom]`` pixels off the edges of ``img``."""
    if not crop or not any(crop):
        return img
    left, top, right, bottom = crop
    box = (left, top, img.width - right, img.height - bottom)
    if box[2] <= box[0] or box[3] <= box[1]:
        raise CaptureError(
            f"crop {list(crop)} leaves nothing of a {img.width}x{img.height} capture"
        )
    return img.crop(box)


def scale_to_native(img: Image.Image, native_size: Sequence[int] | None) -> Image.Image:
    """Resize to ``native_size`` with nearest-neighbour so 1 Spectrum pixel = 1 pixel."""
    if not native_size:
        return img
    size = (int(native_size[0]), int(native_size[1]))
    if img.size == size:
        return img
    return img.resize(size, Image.NEAREST)


def postprocess(
    img: Image.Image,
    crop: Sequence[int] | None = None,
    native_size: Sequence[int] | None = None,
) -> Image.Image:
    """Apply the configured crop then native-size scaling to a raw capture."""
    return scale_to_native(apply_crop(img, crop), native_size)


def is_blank(img: Image.Image) -> bool:
    """True if the image is entirely black."""
    return img.convert("RGB").getbbox() is None


def changed_fraction(a: Image.Image, b: Image.Image) -> float:
    """Fraction of pixels (0..1) that differ between two images."""
    if a.size != b.size:
        return 1.0
    diff = ImageChops.difference(a.convert("RGB"), b.convert("RGB"))
    r, g, b_ = diff.split()
    any_band = ImageChops.lighter(ImageChops.lighter(r, g), b_)
    unchanged = any_band.histogram()[0]
    total = a.width * a.height
    return (total - unchanged) / total if total else 0.0


def _uniform_runs(img: Image.Image, min_len: int) -> list[list[tuple[int, int]]]:
    """Per row, the maximal single-colour runs ``(left, right)`` at least ``min_len`` wide."""
    width, height = img.size
    # A zero byte at x means pixel x has the same colour as pixel x - 1.
    diff = ImageChops.difference(img, ImageChops.offset(img, 1, 0))
    r, g, b = diff.split()
    same = ImageChops.lighter(ImageChops.lighter(r, g), b).tobytes()
    pattern = re.compile(b"\x00{%d,}" % (min_len - 1))
    rows = []
    for y in range(height):
        row = same[y * width + 1 : (y + 1) * width]
        rows.append([(m.start(), m.end() + 1) for m in pattern.finditer(row)])
    return rows


def detect_display(img: Image.Image) -> Box | None:
    """Find the largest rectangle whose outer rows/columns are one uniform colour.

    That rectangle is the Spectrum display including its border; anything
    outside it (status bar, toolbar) is emulator chrome. Returns
    ``(left, top, right, bottom)`` or None if nothing plausible is found.

    Border rows show up as long single-colour runs sharing the same left and
    right ends. Each common (colour, left, right) is tried as the display's
    horizontal extent, and the tallest span whose first and last rows are such
    runs and whose side columns stay that colour gives the rectangle.
    """
    img = img.convert("RGB")
    width, height = img.size
    if width < _MIN_NATIVE_W or height < _MIN_NATIVE_H:
        return None
    px = img.load()
    rows = _uniform_runs(img, _MIN_NATIVE_W)

    seen: Counter = Counter()
    for y, runs in enumerate(rows):
        for left, right in runs:
            seen[(px[left, y], left, right)] += right - left

    best: Box | None = None
    best_area = 0
    for (colour, left, right), _ in seen.most_common(8):
        span_start = None  # first row of the current stretch with clean side columns
        first_edge = last_edge = None  # first/last full-width border rows in it
        for y in range(height + 1):
            clean = y < height and px[left, y] == colour and px[right - 1, y] == colour
            if clean:
                if span_start is None:
                    span_start, first_edge, last_edge = y, None, None
                if any(l <= left and r >= right for l, r in rows[y]):
                    first_edge = y if first_edge is None else first_edge
                    last_edge = y
                continue
            if span_start is not None and first_edge is not None:
                box_height = last_edge - first_edge + 1
                area = box_height * (right - left)
                if box_height >= _MIN_NATIVE_H and area > best_area:
                    best, best_area = (left, first_edge, right, last_edge + 1), area
            span_start = None
    return best


def detect_scale(img: Image.Image) -> int:
    """Largest integer factor by which ``img`` is a clean nearest-neighbour upscale.

    Factors that would make the native image smaller than a Spectrum screen
    are not considered, so a blank display still reports a sensible scale.
    Returns 1 when the image is unscaled or was scaled with filtering.
    """
    img = img.convert("RGB")
    for scale in range(_MAX_SCALE, 1, -1):
        if img.width % scale or img.height % scale:
            continue
        small_size = (img.width // scale, img.height // scale)
        if small_size[0] < _MIN_NATIVE_W or small_size[1] < _MIN_NATIVE_H:
            continue
        small = img.resize(small_size, Image.NEAREST)
        if ImageChops.difference(img, small.resize(img.size, Image.NEAREST)).getbbox() is None:
            return scale
    return 1
