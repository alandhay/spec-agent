"""Convert any image into the closest ZX Spectrum picture.

The Spectrum shows 256x192 pixels in 15 colours, but each attribute cell may
use only two of them (ink and paper, sharing one brightness). Cells are 8x8
pixels on a plain screen; with the multicolour display routine
(multicolour.py) the middle 24 columns get 8x2 cells.

"Closest" is measured the way the result is judged: blur both the Spectrum
picture and the original as the eye does, and compare them in a perceptual
colour space (Oklab). The converter works in three steps:

1. fit    - decode anything Pillow can read, flatten transparency, honour the
            camera orientation, and resize in linear light to 256x192;
2. dither - choose a colour pair per cell and error-diffuse, row by row, each
            row of cells picking its pair with the error left by the rows
            above included, so neighbouring rows complement each other;
3. refine - direct search: every cell tries other colour pairs and every pixel
            its cell's other colour, keeping whatever brings the blurred
            picture closer to the blurred original.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Callable

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from PIL import Image, ImageOps

from . import _kernels

WIDTH, HEIGHT = 256, 192
COLS, PAIR_ROWS = 32, 96            # attribute columns; rows of 8x2 cells
MC_COL0, MC_COLS = 4, 24            # columns the multicolour routine covers
NORMAL, BRIGHT = 192, 255           # the levels Fuse (and most emulators) display

# Spectrum colour n has blue = bit 0, red = bit 1, green = bit 2.
PALETTE_SRGB = np.array(
    [[(n >> 1 & 1) * level, (n >> 2 & 1) * level, (n & 1) * level] for level in (NORMAL, BRIGHT) for n in range(8)],
    dtype=float,
)

Progress = Callable[..., None]


def to_linear(srgb):
    c = np.asarray(srgb, dtype=float) / 255.0
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def to_srgb(linear):
    c = np.clip(linear, 0, 1)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * c ** (1 / 2.4) - 0.055) * 255.0


def oklab(linear):
    lin = np.clip(linear, 0, None)
    m = np.array([[0.4122214708, 0.5363325363, 0.0514459929],
                  [0.2119034982, 0.6806995451, 0.1073969566],
                  [0.0883024619, 0.2817188376, 0.6299787005]])
    lms = np.cbrt(lin @ m.T)
    m2 = np.array([[0.2104542553, 0.7936177850, -0.0040720468],
                   [1.9779984951, -2.4285922050, 0.4505937099],
                   [0.0259040371, 0.7827717662, -0.8086757660]])
    return lms @ m2.T


PALETTE = to_linear(PALETTE_SRGB)          # 16 x 3, linear light
PALETTE_LAB = oklab(PALETTE)


def _pairs() -> list[tuple[int, int]]:
    """Every distinct pair of colours that can share a cell (same brightness)."""
    pairs, seen = [], set()
    for bright in (0, 1):
        for a in range(8):
            for b in range(a, 8):
                key = (tuple(PALETTE_SRGB[bright * 8 + a]), tuple(PALETTE_SRGB[bright * 8 + b]))
                if key not in seen:
                    seen.add(key)
                    pairs.append((bright * 8 + a, bright * 8 + b))
    return pairs


PAIRS = _pairs()
PAIR_ARRAY = np.array(PAIRS, dtype=np.int64)


def cell_heights(multicolour: bool) -> np.ndarray:
    """Cell height in pixels for each of the 32 columns."""
    heights = np.full(COLS, 8, dtype=np.int64)
    if multicolour:
        heights[MC_COL0 : MC_COL0 + MC_COLS] = 2
    return heights


# ------------------------------------------------------------------- fitting --

def open_image(source) -> Image.Image:
    """Open a path, bytes or file object as an RGB image, whatever its format or depth."""
    if isinstance(source, (bytes, bytearray)):
        source = io.BytesIO(source)
    img = Image.open(source)
    try:
        img.seek(0)                                     # first frame of an animation
    except EOFError:
        pass
    img = ImageOps.exif_transpose(img)
    if max(img.size) > 4096:                            # keep memory sensible for huge photos
        img.thumbnail((4096, 4096), Image.LANCZOS)
    if img.mode in ("I", "F") or img.mode.startswith("I;"):
        # 16/32-bit greyscale: scale the actual range to 8 bits.
        data = np.asarray(img, dtype=float)
        lo, hi = float(data.min()), float(data.max())
        img = Image.fromarray(((data - lo) / (hi - lo or 1) * 255).astype(np.uint8), "L")
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (0, 0, 0, 255))
        img = Image.alpha_composite(background, rgba)
    return img.convert("RGB")


def fit_image(img: Image.Image, fit: str = "fit") -> np.ndarray:
    """Resize to the Spectrum screen in linear light; returns 192 x 256 x 3 linear RGB.

    ``fit`` keeps the whole picture (black bars fill the rest); ``fill`` crops
    the longer side so the screen is covered; ``stretch`` ignores the shape.
    """
    w, h = img.size
    if fit == "stretch":
        new_w, new_h = WIDTH, HEIGHT
    else:
        scale = (max if fit == "fill" else min)(WIDTH / w, HEIGHT / h)
        new_w, new_h = max(1, round(w * scale)), max(1, round(h * scale))
    lin = to_linear(np.asarray(img))
    small = np.stack(
        [np.asarray(Image.fromarray(lin[..., ch].astype(np.float32), "F").resize((new_w, new_h), Image.LANCZOS))
         for ch in range(3)], axis=-1)
    small = np.clip(small, 0, 1)
    if fit == "fill":
        x0, y0 = (new_w - WIDTH) // 2, (new_h - HEIGHT) // 2
        return small[y0 : y0 + HEIGHT, x0 : x0 + WIDTH].astype(float)
    canvas = np.zeros((HEIGHT, WIDTH, 3))
    x0, y0 = (WIDTH - new_w) // 2, (HEIGHT - new_h) // 2
    canvas[y0 : y0 + new_h, x0 : x0 + new_w] = small
    return canvas


# ------------------------------------------------------------ the eye model --
# The eye resolves fine detail in brightness but blends neighbouring pixels'
# colour and overall tone. The search filters both pictures with a kernel that
# has a sharp centre (so a lone bright dot on black counts as the visible
# speck it is) and a wide skirt (so the average tone over a patch must match).

EYE_RADIUS = _kernels.R
OPPONENT = np.array([[0.2126, 0.7152, 0.0722], [1.0, -1.0, 0.0], [0.5, 0.5, -1.0]])
CHANNEL_WEIGHT = np.sqrt(np.array([1.0, 0.6, 0.4]))
FLARE = 0.012                       # stray light: nobody sees 1% grey as far from black


def gaussian2d(sigma: float, radius: int = EYE_RADIUS) -> np.ndarray:
    g = np.exp(-0.5 * (np.arange(-radius, radius + 1) / sigma) ** 2)
    g /= g.sum()
    return np.outer(g, g)


EYE_KERNEL = 0.55 * gaussian2d(0.75) + 0.45 * gaussian2d(2.4)


def filter2(img: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    """Convolve an H x W (x C) array with a small square kernel (edges repeated)."""
    r = kernel.shape[0] // 2
    pad = [(r, r), (r, r)] + [(0, 0)] * (img.ndim - 2)
    padded = np.pad(img, pad, mode="edge")
    out = np.zeros(img.shape)
    for i in range(kernel.shape[0]):
        for j in range(kernel.shape[1]):
            out += kernel[i, j] * padded[i : i + img.shape[0], j : j + img.shape[1]]
    return out


def blur(img: np.ndarray, sigma: float) -> np.ndarray:
    return filter2(img, gaussian2d(sigma, max(2, int(3 * sigma + 0.5))))


def perceived_error(target: np.ndarray, picture_linear: np.ndarray) -> float:
    """How different the picture looks from the original: 0 = identical, 1 = black vs white.

    RMS Oklab difference (with a little flare added, as on any real screen)
    between the two pictures, blurred at a fine and a coarse scale: the fine
    scale sees grain and lost detail, the coarse one sees wrong tone or colour.
    """
    total = 0.0
    for sigma in (1.0, 3.0):
        d = oklab(blur(target, sigma) + FLARE) - oklab(blur(picture_linear, sigma) + FLARE)
        total += (d ** 2).sum(-1).mean()
    return float(np.sqrt(total / 2))


def error_of(target: np.ndarray, indices: np.ndarray) -> float:
    return perceived_error(target, PALETTE[indices])


def expose(target: np.ndarray, gamma: float) -> np.ndarray:
    """Lift (gamma < 1) or deepen the tones of a linear image, keeping hue and white."""
    if gamma == 1.0:
        return target
    y = target @ OPPONENT[0]
    scale = np.where(y > 1e-6, np.power(np.maximum(y, 1e-6), gamma - 1.0), 0.0)
    return np.clip(target * scale[..., None], 0, 1)


# ------------------------------------------------------- dithering, refinement --
# The loops themselves are compiled (see _kernels.py); these drive them a band of
# rows at a time so progress can be shown.

NOISE_PENALTY = 0.02


def dither(target: np.ndarray, heights: np.ndarray, progress: Progress | None = None):
    """Pick pairs and error-diffuse, top to bottom. Returns (indices, pairs[96, 32, 2])."""
    work = target.copy()
    out = np.zeros((HEIGHT, WIDTH), dtype=np.int64)
    pairs = np.zeros((PAIR_ROWS, COLS, 2), dtype=np.int64)
    for y in range(0, HEIGHT, 8):
        _kernels.dither_rows(y, y + 8, work, heights, PALETTE, PALETTE_LAB, PAIR_ARRAY, NOISE_PENALTY, out, pairs)
        if progress:
            progress(indices=out, row=y + 8)
    return out, pairs


def refine(target: np.ndarray, indices: np.ndarray, pairs: np.ndarray, heights: np.ndarray,
           max_passes: int = 8, progress: Progress | None = None):
    """Direct search against the eye model. Returns (indices, pairs)."""
    R = EYE_RADIUS
    G = EYE_KERNEL
    pal = (PALETTE @ OPPONENT.T) * CHANNEL_WEIGHT
    want = (target @ OPPONENT.T) * CHANNEL_WEIGHT
    # Errors in dark areas are easier to see, so they count for more.
    weight = 1.0 / (blur(target @ OPPONENT[0], 1.5) + 0.15)
    wpad = np.pad(weight, R, mode="edge")
    s2 = (sliding_window_view(wpad, (2 * R + 1, 2 * R + 1)) * (G * G)).sum(axis=(-1, -2))
    indices = indices.astype(np.int64).copy()
    pairs = pairs.astype(np.int64).copy()
    err = np.pad(filter2(pal[indices] - want, G), ((R, R), (R, R), (0, 0)))    # filtered error, padded

    score = error_of(target, indices)
    for n in range(1, max_passes + 1):
        cells_changed = 0
        for y in range(0, HEIGHT, 8):
            cells_changed += _kernels.refine_rows(y, y + 8, heights, indices, pairs, err, wpad, s2, pal, G, PAIR_ARRAY)
            if progress:
                progress(indices=indices, row=y + 8, refine_pass=n)
        flipped = _kernels.flip_pixels(indices, pairs, err, wpad, s2, pal, G)
        flipped += _kernels.flip_pixels(indices, pairs, err, wpad, s2, pal, G)
        new_score = error_of(target, indices)
        if progress:
            progress(indices=indices, row=HEIGHT, refine_pass=n, pass_done=True, score=new_score,
                     cells_changed=cells_changed, pixels_flipped=flipped)
        if score - new_score < 0.0001:              # converged
            break
        score = new_score
    return indices, pairs


# --------------------------------------------------------------------- output --

def encode(indices: np.ndarray, pairs: np.ndarray) -> tuple[bytes, np.ndarray]:
    """Spectrum bitmap (6144 bytes, screen order) and an attribute byte per 8x2 cell [96, 32]."""
    attrs = np.zeros((PAIR_ROWS, COLS), dtype=np.uint8)
    bits = np.zeros((HEIGHT, WIDTH), dtype=np.uint8)
    for r in range(PAIR_ROWS):
        for c in range(COLS):
            a, b = pairs[r, c]
            block = indices[r * 2 : r * 2 + 2, c * 8 : c * 8 + 8]
            if a != b:                                  # ink = b, paper = a
                bits[r * 2 : r * 2 + 2, c * 8 : c * 8 + 8] = block == b
            attrs[r, c] = (0x40 if max(a, b) >= 8 else 0) | (a & 7) << 3 | (b & 7)
    packed = np.packbits(bits, axis=1)
    bitmap = bytearray(6144)
    for y in range(HEIGHT):
        addr = (y & 0xC0) << 5 | (y & 0x07) << 8 | (y & 0x38) << 2
        bitmap[addr : addr + 32] = packed[y].tobytes()
    return bytes(bitmap), attrs


def render(indices: np.ndarray) -> np.ndarray:
    """The picture as an emulator shows it: sRGB uint8, 192 x 256 x 3."""
    return PALETTE_SRGB[indices].astype(np.uint8)


def blended(indices: np.ndarray) -> np.ndarray:
    """The picture as the eye sees it from a distance: sRGB uint8."""
    return to_srgb(blur(PALETTE[indices], 1.0)).round().astype(np.uint8)
