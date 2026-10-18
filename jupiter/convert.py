"""Convert a photograph of Jupiter into ZX Spectrum screen data.

The Spectrum shows 256x192 pixels in 15 colours, with only two colours (ink
and paper, sharing one brightness) allowed in each attribute cell. This
converter gets the most out of that:

* the photo is resized in linear light, so tones are not darkened;
* for every cell it picks the ink/paper pair whose *mixes* best cover the
  cell's colours (a dither can show any blend along the line between two
  colours), judged in the perceptual Oklab space, with a penalty for pairs
  that would dither noisily;
* pixels are then error-diffusion dithered in linear light between each
  cell's two colours, with the error carried across cell boundaries.

Cells are 8x8 for a standard screen, or 8x2 inside the multicolour area
(see multicolour.py, which changes the attributes every two scan lines).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

HERE = Path(__file__).resolve().parent
WIDTH, HEIGHT = 256, 192
MC_COL0, MC_COLS = 4, 24            # multicolour area: character columns 4..27
NORMAL, BRIGHT = 192, 255           # the levels Fuse (and most emulators) display

# Spectrum colour n has blue = bit 0, red = bit 1, green = bit 2.
PALETTE_SRGB = np.array(
    [[(n >> 1 & 1) * level, (n >> 2 & 1) * level, (n & 1) * level] for level in (NORMAL, BRIGHT) for n in range(8)],
    dtype=float,
)


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


# Jupiter is cream, tan, rust and grey-blue: stray green, blue or magenta pixels look wrong
# close up even when they average out, so only these Spectrum colours are used.
ALLOWED = (0, 2, 5, 6, 7)           # black, red, cyan, yellow, white


def candidate_pairs():
    """All (colour index, colour index) pairs sharing a brightness, without duplicates."""
    pairs, seen = [], set()
    for bright in (0, 1):
        for a in ALLOWED:
            for b in ALLOWED:
                if b < a:
                    continue
                key = (tuple(PALETTE_SRGB[bright * 8 + a]), tuple(PALETTE_SRGB[bright * 8 + b]))
                if key not in seen:
                    seen.add(key)
                    pairs.append((bright * 8 + a, bright * 8 + b))
    return pairs


PAIRS = candidate_pairs()


def load_planet(path: Path, disc_width: int, saturation: float, contrast: float, sharpen: int,
                limb_floor: float = 0.3, edge: float = 0.99, disc_threshold: int = 75) -> np.ndarray:
    """Return a HEIGHT x WIDTH x 3 linear-light image with the planet centred on black."""
    src = Image.open(path).convert("RGB")
    # Crop to the visibly lit disc (the faint outer glow would dither to stray dots).
    grey = np.asarray(src.convert("L"))
    ys, xs = np.nonzero(grey > disc_threshold)
    src = src.crop((xs.min(), ys.min(), xs.max() + 1, ys.max() + 1))
    disc_height = round(disc_width * src.height / src.width)

    lin = to_linear(np.asarray(src))
    small = np.stack(
        [np.asarray(Image.fromarray(lin[..., ch].astype(np.float32), "F").resize((disc_width, disc_height), Image.LANCZOS))
         for ch in range(3)], axis=-1)
    small = np.clip(small, 0, 1)

    # Tone work is done on the display-referred image.
    img = Image.fromarray(to_srgb(small).round().astype(np.uint8), "RGB")
    if sharpen:
        img = img.filter(ImageFilter.UnsharpMask(radius=1.2, percent=sharpen, threshold=0))
    arr = np.asarray(img, dtype=float) / 255.0
    luma = arr @ np.array([0.299, 0.587, 0.114])
    arr = luma[..., None] + (arr - luma[..., None]) * saturation
    arr = 0.5 + (arr - 0.5) * contrast
    planet = to_linear(np.clip(arr, 0, 1) * 255.0)

    # The photo fades to black at the limb. Dithered, that becomes a ragged scatter of
    # dots, so lift the limb part of the way back up and cut the disc off cleanly.
    yy, xx = np.mgrid[0:disc_height, 0:disc_width]
    r2 = ((xx + 0.5 - disc_width / 2) / (disc_width / 2)) ** 2 + ((yy + 0.5 - disc_height / 2) / (disc_height / 2)) ** 2
    mu = np.sqrt(np.clip(1 - r2 / edge**2, 0, 1))
    planet = np.clip(planet / (limb_floor + (1 - limb_floor) * mu**0.55)[..., None], 0, 1)
    planet[r2 > edge**2] = 0

    canvas = np.zeros((HEIGHT, WIDTH, 3))
    x0, y0 = (WIDTH - disc_width) // 2, (HEIGHT - disc_height) // 2
    canvas[y0 : y0 + disc_height, x0 : x0 + disc_width] = planet
    return canvas


def choose_pairs(target: np.ndarray, cell_h: int, noise_penalty: float, smooth: float = 0.0,
                 space: np.ndarray | None = None) -> np.ndarray:
    """Best palette pair for each cell_h x 8 cell: array [rows, 32, 2] of palette indices.

    ``target`` is any whole number of cell rows (H x 256 x 3). ``smooth`` shares each
    cell's cost with its left and right neighbours so runs of cells agree on a pair
    (Jupiter's bands run sideways; without this the picture breaks into 8-pixel dashes).
    ``space`` marks pixels of black sky: a cell containing any must have black as one
    of its colours, which keeps the planet's edge clean.
    """
    rows = target.shape[0] // cell_h

    def cells_of(a):
        return a.reshape(rows, cell_h, 32, 8, -1).transpose(0, 2, 1, 3, 4).reshape(rows, 32, cell_h * 8, -1)

    cells = cells_of(target)
    cells_lab = oklab(cells)
    costs = np.zeros((len(PAIRS), rows, 32))
    for n, (a, b) in enumerate(PAIRS):
        c1, c2 = PALETTE[a], PALETTE[b]
        d = c2 - c1
        dd = float(d @ d)
        t = np.clip(((cells - c1) @ d) / dd, 0, 1) if dd else np.zeros(cells.shape[:-1])
        mix_lab = oklab(c1 + t[..., None] * d)
        cost = ((cells_lab - mix_lab) ** 2).sum(axis=(-1, -2))
        # A blend of two very different colours is grainy: prefer close pairs when they will do.
        contrast = float(((PALETTE_LAB[a] - PALETTE_LAB[b]) ** 2).sum())
        costs[n] = cost + noise_penalty * contrast * (t * (1 - t)).sum(axis=-1)
    if smooth:
        kernel = np.array([smooth / 2, smooth, 1.0, smooth, smooth / 2])
        padded = np.pad(costs, ((0, 0), (0, 0), (2, 2)), mode="edge")
        costs = sum(k * padded[:, :, i : i + 32] for i, k in enumerate(kernel))
    if space is not None:
        has_space = cells_of(space[..., None].astype(float)).max(axis=(-1, -2)) > 0
        no_black = np.array([PALETTE_SRGB[a].max() > 0 for a, _ in PAIRS])     # a <= b, black sorts first
        costs[np.ix_(no_black, np.arange(rows), np.arange(32))] += np.where(has_space, np.inf, 0)[None]
    best = costs.argmin(axis=0)
    return np.array(PAIRS)[best]


def dither(target: np.ndarray, cell_h: int, noise_penalty: float, smooth: float,
           fixed: np.ndarray | None = None):
    """Choose colour pairs and dither, one cell row at a time, top to bottom.

    Each row of cells picks its pairs from the picture *plus the error the rows above
    could not show*, so neighbouring rows complement each other: where one row can
    only offer white and yellow, the next leans to yellow and red, and from a distance
    the eye sees the tan in between. With 8x2 cells that gives four colours in every
    8x4 patch. ``fixed`` supplies pairs for columns outside the multicolour area.

    Returns (palette index per pixel, pairs [rows, 32, 2]).
    """
    weights = np.array([0.30, 0.59, 0.11])
    work = target.copy()
    out = np.zeros((HEIGHT, WIDTH), dtype=int)
    pairs = np.zeros((HEIGHT // cell_h, 32, 2), dtype=int)
    outside = np.ones(32, dtype=bool)
    outside[MC_COL0 : MC_COL0 + MC_COLS] = False
    for y in range(HEIGHT):
        if y % cell_h == 0:
            band = np.clip(work[y : y + cell_h], -0.25, 1.25)
            sky = target[y : y + cell_h].max(axis=-1) == 0
            band[sky] = 0
            row = choose_pairs(band, cell_h, noise_penalty, smooth, sky)[0]
            if fixed is not None:
                row[outside] = fixed[y // cell_h][outside]
            pairs[y // cell_h] = row
        xs = range(WIDTH) if y % 2 == 0 else range(WIDTH - 1, -1, -1)
        step = 1 if y % 2 == 0 else -1
        for x in xs:
            a, b = pairs[y // cell_h, x // 8]
            want = work[y, x]
            if a == b:
                pick = a
            else:
                da = ((want - PALETTE[a]) ** 2 * weights).sum()
                db = ((want - PALETTE[b]) ** 2 * weights).sum()
                pick = a if da <= db else b
            out[y, x] = pick
            if target[y, x].max() == 0:
                continue                                  # black space absorbs no error
            err = np.clip(want - PALETTE[pick], -0.35, 0.35)
            for dx, dy, w in ((step, 0, 7 / 16), (-step, 1, 3 / 16), (0, 1, 5 / 16), (step, 1, 1 / 16)):
                xx, yy = x + dx, y + dy
                if 0 <= xx < WIDTH and yy < HEIGHT and target[yy, xx].max() > 0:
                    work[yy, xx] += err * w
    return out, pairs


# ---------------------------------------------------------------------------
# Refinement: direct search against a model of what the eye sees.
#
# The eye blurs neighbouring pixels together, so what matters is the *blurred*
# picture. Starting from the error-diffused result, every cell tries every
# colour pair, and every pixel tries its cell's other colour, keeping any change
# that brings the blurred Spectrum picture closer to the blurred photograph
# (compared in linear light, as luminance plus two colour-difference channels,
# with dark areas weighted up as the eye is more sensitive there).

EYE_RADIUS = 4
OPPONENT = np.array([[0.2126, 0.7152, 0.0722], [1.0, -1.0, 0.0], [0.5, 0.5, -1.0]])
CHANNEL_WEIGHT = np.sqrt(np.array([1.0, 0.6, 0.4]))


def eye_kernel(sigma: float) -> np.ndarray:
    g = np.exp(-0.5 * (np.arange(-EYE_RADIUS, EYE_RADIUS + 1) / sigma) ** 2)
    g /= g.sum()
    return np.outer(g, g)


def blur(img: np.ndarray, sigma: float) -> np.ndarray:
    g = np.exp(-0.5 * (np.arange(-EYE_RADIUS, EYE_RADIUS + 1) / sigma) ** 2)
    g /= g.sum()
    out = np.apply_along_axis(lambda v: np.convolve(v, g, mode="same"), 1, img)
    return np.apply_along_axis(lambda v: np.convolve(v, g, mode="same"), 0, out)


def perceived_error(target: np.ndarray, indices: np.ndarray, sigma: float = 1.3) -> float:
    """RMS Oklab difference between the blurred picture and the blurred photo, over the disc."""
    disc = target.max(-1) > 0
    d = oklab(blur(target, sigma)) - oklab(blur(PALETTE[indices], sigma))
    return float(np.sqrt((d[disc] ** 2).sum(-1).mean()))


def refine(target: np.ndarray, indices: np.ndarray, pairs: np.ndarray, cell_h: int,
           passes: int = 3, sigma: float = 1.3, log=print) -> tuple[np.ndarray, np.ndarray]:
    R, K = EYE_RADIUS, 2 * EYE_RADIUS + 1
    G = eye_kernel(sigma)
    pal = (PALETTE @ OPPONENT.T) * CHANNEL_WEIGHT
    want = (target @ OPPONENT.T) * CHANNEL_WEIGHT
    sky = target.max(-1) == 0
    weight = 1.0 / (blur(target @ OPPONENT[0], sigma) + 0.15)
    wpad = np.pad(weight, R, mode="edge")
    indices = indices.copy()
    pairs = pairs.copy()
    err = np.pad(blur(pal[indices] - want, sigma), ((R, R), (R, R), (0, 0)))     # blurred error, padded
    black_pairs = [(a, b) for a, b in PAIRS if PALETTE_SRGB[a].max() == 0]

    def gradient(e, y, x):
        """For a pixel: v and s such that changing its colour by d costs 2 d.v + |d|^2 s."""
        a = wpad[y : y + K, x : x + K] * G
        return np.tensordot(a, e, axes=([0, 1], [0, 1])), float((a * G).sum())

    def sweep_pixels() -> int:
        changed = 0
        for y in range(HEIGHT):
            for x in range(WIDTH):
                if sky[y, x]:
                    continue
                a, b = pairs[y // cell_h, x // 8]
                if a == b:
                    continue
                cur = indices[y, x]
                other = b if cur == a else a
                patch = err[y : y + K, x : x + K]
                v, s2 = gradient(patch, y, x)
                d = pal[other] - pal[cur]
                if 2 * (d @ v) + (d @ d) * s2 < -1e-12:
                    indices[y, x] = other
                    patch += G[..., None] * d
                    changed += 1
        return changed

    def search_pairs() -> int:
        changed = 0
        for cy in range(HEIGHT // cell_h):
            y0 = cy * cell_h
            for cx in range(32):
                x0 = cx * 8
                cell_sky = sky[y0 : y0 + cell_h, x0 : x0 + 8]
                if cell_sky.all():
                    continue
                region = err[y0 : y0 + cell_h + 2 * R, x0 : x0 + 8 + 2 * R]
                current = indices[y0 : y0 + cell_h, x0 : x0 + 8]
                best = (-1e-9, None, None, None)
                for a, b in (black_pairs if cell_sky.any() else PAIRS):
                    e = region.copy()
                    cols = current.copy()
                    total = 0.0
                    for _ in range(2):
                        for yy in range(cell_h):
                            for xx in range(8):
                                if cell_sky[yy, xx]:
                                    continue
                                patch = e[yy : yy + K, xx : xx + K]
                                v, s2 = gradient(patch, y0 + yy, x0 + xx)
                                cur = cols[yy, xx]
                                da, db = pal[a] - pal[cur], pal[b] - pal[cur]
                                ca = 2 * (da @ v) + (da @ da) * s2
                                cb = 2 * (db @ v) + (db @ db) * s2
                                pick, d, c = (a, da, ca) if ca <= cb else (b, db, cb)
                                if pick != cur:
                                    cols[yy, xx] = pick
                                    patch += G[..., None] * d
                                    total += c
                    if total < best[0]:
                        best = (total, (a, b), cols, e)
                if best[1] is not None:
                    if tuple(pairs[cy, cx]) != best[1]:
                        changed += 1
                    pairs[cy, cx] = best[1]
                    current[...] = best[2]
                    region[...] = best[3]
        return changed

    for n in range(passes):
        cells = search_pairs()
        pixels = sweep_pixels() + sweep_pixels()
        log(f"  refine pass {n + 1}: {cells} cells changed pair, {pixels} pixels flipped, "
            f"perceived error {perceived_error(target, indices):.4f}")
    return indices, pairs


def encode(indices: np.ndarray, pairs: np.ndarray, cell_h: int) -> tuple[bytes, np.ndarray]:
    """Spectrum bitmap (6144 bytes in screen order) and attribute byte per cell."""
    rows = HEIGHT // cell_h
    attrs = np.zeros((rows, 32), dtype=np.uint8)
    bits = np.zeros((HEIGHT, WIDTH), dtype=np.uint8)
    for r in range(rows):
        for c in range(32):
            a, b = pairs[r, c]
            block = indices[r * cell_h : (r + 1) * cell_h, c * 8 : (c + 1) * 8]
            # ink = b, paper = a; a plain cell is all paper.
            bits[r * cell_h : (r + 1) * cell_h, c * 8 : (c + 1) * 8] = block == b if a != b else 0
            bright = 0x40 if max(a, b) >= 8 else 0
            attrs[r, c] = bright | (a & 7) << 3 | (b & 7)
    packed = np.packbits(bits, axis=1)                    # 192 x 32 bytes, top to bottom
    bitmap = bytearray(6144)
    for y in range(HEIGHT):
        addr = (y & 0xC0) << 5 | (y & 0x07) << 8 | (y & 0x38) << 2
        bitmap[addr : addr + 32] = packed[y].tobytes()
    return bytes(bitmap), attrs


def render(indices: np.ndarray) -> np.ndarray:
    """The picture as the emulator shows it (sRGB, uint8)."""
    return PALETTE_SRGB[indices].astype(np.uint8)


def save_previews(indices: np.ndarray, name: str) -> None:
    rgb = render(indices)
    Image.fromarray(rgb).resize((WIDTH * 3, HEIGHT * 3), Image.NEAREST).save(HERE / "build" / f"{name}.png")
    # What the eye sees from a distance: blend neighbouring pixels in linear light.
    lin = PALETTE[indices]
    k = np.exp(-0.5 * (np.arange(-3, 4) / 1.1) ** 2)
    k /= k.sum()
    blurred = np.apply_along_axis(lambda v: np.convolve(v, k, mode="same"), 1, lin)
    blurred = np.apply_along_axis(lambda v: np.convolve(v, k, mode="same"), 0, blurred)
    Image.fromarray(to_srgb(blurred).round().astype(np.uint8)).resize((WIDTH * 3, HEIGHT * 3), Image.BICUBIC).save(
        HERE / "build" / f"{name}_blended.png")


def convert(source: Path, cell_h: int, disc_width: int = 192, saturation: float = 1.25, contrast: float = 1.08,
            sharpen: int = 80, noise_penalty: float = 0.02, smooth: float = 0.0, passes: int = 3, log=print):
    """Full conversion. Inside the multicolour columns cells are cell_h tall; outside, 8."""
    target = load_planet(source, disc_width, saturation, contrast, sharpen)
    fixed = None
    if cell_h != 8:
        # Outside the multicolour area the hardware cells are 8 tall.
        fixed = np.repeat(choose_pairs(target, 8, noise_penalty, 0, target.max(axis=-1) == 0), 8 // cell_h, axis=0)
    indices, pairs = dither(target, cell_h, noise_penalty, smooth, fixed)
    log(f"  error diffusion: perceived error {perceived_error(target, indices):.4f}")
    if passes:
        indices, pairs = refine(target, indices, pairs, cell_h, passes, log=log)
    bitmap, attrs = encode(indices, pairs, cell_h)
    return target, indices, bitmap, attrs


if __name__ == "__main__":
    source = HERE / "source" / "hubble2019.jpg"
    (HERE / "build").mkdir(exist_ok=True)
    for name, cell_h in (("multicolour", 2),):
        print(name)
        target, indices, bitmap, attrs = convert(source, cell_h)
        save_previews(indices, name)
        print(f"{name}: {len(set(attrs.flatten()))} distinct attributes, "
              f"perceived error {perceived_error(target, indices):.4f}")
    Image.fromarray(to_srgb(target).round().astype(np.uint8)).resize((WIDTH * 3, HEIGHT * 3), Image.BICUBIC).save(
        HERE / "build" / "target.png")
