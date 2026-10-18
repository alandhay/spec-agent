"""Compiled inner loops for convert.py (numba). See convert.py for what they are for."""

from __future__ import annotations

import numpy as np
from numba import njit, prange

H, W, COLS = 192, 256, 32
R, K = 6, 13                    # eye-model blur radius and kernel size


@njit(cache=True)
def _oklab(r, g, b):
    if r < 0.0:
        r = 0.0
    if g < 0.0:
        g = 0.0
    if b < 0.0:
        b = 0.0
    l = np.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b)
    m = np.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b)
    s = np.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b)
    return (0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
            1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
            0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s)


@njit(cache=True)
def _best_pair(work, y0, x0, h, pal, pal_lab, pair_arr, noise):
    """The pair whose blends lie closest (in Oklab) to the cell's pixels."""
    n = h * 8
    px = np.empty((n, 3))
    lab = np.empty((n, 3))
    i = 0
    for yy in range(h):
        for xx in range(8):
            for ch in range(3):
                v = work[y0 + yy, x0 + xx, ch]
                px[i, ch] = min(1.25, max(-0.25, v))
            lab[i, 0], lab[i, 1], lab[i, 2] = _oklab(px[i, 0], px[i, 1], px[i, 2])
            i += 1
    best, best_cost = 0, 1e30
    for p in range(pair_arr.shape[0]):
        a, b = pair_arr[p, 0], pair_arr[p, 1]
        d0, d1, d2 = pal[b, 0] - pal[a, 0], pal[b, 1] - pal[a, 1], pal[b, 2] - pal[a, 2]
        dd = d0 * d0 + d1 * d1 + d2 * d2
        contrast = ((pal_lab[a, 0] - pal_lab[b, 0]) ** 2 + (pal_lab[a, 1] - pal_lab[b, 1]) ** 2
                    + (pal_lab[a, 2] - pal_lab[b, 2]) ** 2)
        cost = 0.0
        for i in range(n):
            t = 0.0
            if dd > 0.0:
                t = ((px[i, 0] - pal[a, 0]) * d0 + (px[i, 1] - pal[a, 1]) * d1 + (px[i, 2] - pal[a, 2]) * d2) / dd
                t = min(1.0, max(0.0, t))
            l, m, s = _oklab(pal[a, 0] + t * d0, pal[a, 1] + t * d1, pal[a, 2] + t * d2)
            cost += (lab[i, 0] - l) ** 2 + (lab[i, 1] - m) ** 2 + (lab[i, 2] - s) ** 2 + noise * contrast * t * (1 - t)
            if cost >= best_cost:
                break
        if cost < best_cost:
            best_cost, best = cost, p
    return best


@njit(cache=True)
def dither_rows(y_start, y_end, work, heights, pal, pal_lab, pair_arr, noise, out, pairs):
    """Error-diffuse rows y_start..y_end-1, choosing each cell's pair as its first row is reached."""
    for y in range(y_start, y_end):
        for c in range(COLS):
            h = heights[c]
            if y % h == 0:
                p = _best_pair(work, y, c * 8, h, pal, pal_lab, pair_arr, noise)
                for r in range(h // 2):
                    pairs[y // 2 + r, c, 0] = pair_arr[p, 0]
                    pairs[y // 2 + r, c, 1] = pair_arr[p, 1]
        step = 1 if y % 2 == 0 else -1
        x = 0 if step == 1 else W - 1
        for _ in range(W):
            a, b = pairs[y // 2, x // 8, 0], pairs[y // 2, x // 8, 1]
            da = db = 0.0
            for ch, wt in enumerate((0.30, 0.59, 0.11)):
                da += wt * (work[y, x, ch] - pal[a, ch]) ** 2
                db += wt * (work[y, x, ch] - pal[b, ch]) ** 2
            pick = a if da <= db else b
            out[y, x] = pick
            for ch in range(3):
                e = min(0.35, max(-0.35, work[y, x, ch] - pal[pick, ch]))
                if 0 <= x + step < W:
                    work[y, x + step, ch] += e * 7 / 16
                if y + 1 < H:
                    work[y + 1, x, ch] += e * 5 / 16
                    if 0 <= x - step < W:
                        work[y + 1, x - step, ch] += e * 3 / 16
                    if 0 <= x + step < W:
                        work[y + 1, x + step, ch] += e * 1 / 16
            x += step


@njit(cache=True)
def _gradient(e, ey, ex, wpad, G, y, x):
    """v such that changing pixel (y, x) by colour d changes the cost by 2 d.v + |d|^2 s."""
    v0 = v1 = v2 = 0.0
    for i in range(K):
        for j in range(K):
            a = wpad[y + i, x + j] * G[i, j]
            v0 += a * e[ey + i, ex + j, 0]
            v1 += a * e[ey + i, ex + j, 1]
            v2 += a * e[ey + i, ex + j, 2]
    return v0, v1, v2


@njit(cache=True)
def _apply(e, ey, ex, G, d0, d1, d2):
    for i in range(K):
        for j in range(K):
            g = G[i, j]
            e[ey + i, ex + j, 0] += g * d0
            e[ey + i, ex + j, 1] += g * d1
            e[ey + i, ex + j, 2] += g * d2


@njit(cache=True)
def _trial(e, cols, a, b, h, y0, x0, wpad, s2, pal, G):
    """Re-dither one cell with the pair (a, b) against the local error; returns the cost change."""
    total = 0.0
    for sweep in range(3):
        moved = False
        for yy in range(h):
            for xx in range(8):
                v0, v1, v2 = _gradient(e, yy, xx, wpad, G, y0 + yy, x0 + xx)
                cur = cols[yy, xx]
                s = s2[y0 + yy, x0 + xx]
                a0, a1, a2 = pal[a, 0] - pal[cur, 0], pal[a, 1] - pal[cur, 1], pal[a, 2] - pal[cur, 2]
                b0, b1, b2 = pal[b, 0] - pal[cur, 0], pal[b, 1] - pal[cur, 1], pal[b, 2] - pal[cur, 2]
                ca = 2 * (a0 * v0 + a1 * v1 + a2 * v2) + (a0 * a0 + a1 * a1 + a2 * a2) * s
                cb = 2 * (b0 * v0 + b1 * v1 + b2 * v2) + (b0 * b0 + b1 * b1 + b2 * b2) * s
                if ca <= cb:
                    if a != cur:
                        cols[yy, xx] = a
                        _apply(e, yy, xx, G, a0, a1, a2)
                        total += ca
                        moved = True
                elif b != cur:
                    cols[yy, xx] = b
                    _apply(e, yy, xx, G, b0, b1, b2)
                    total += cb
                    moved = True
        if not moved:
            break
    return total


@njit(cache=True, parallel=True)
def refine_rows(y_start, y_end, heights, indices, pairs, err, wpad, s2, pal, G, pair_arr):
    """For each cell starting in rows y_start..y_end-1, try every pair; keep the best.

    ``err`` is the blurred error (padded by R); ``pal`` is the palette in the
    weighted opponent space the error is measured in. The trials for one cell
    are independent, so they run in parallel. Returns cells changed.
    """
    npairs = pair_arr.shape[0]
    changed = 0
    for y0 in range(y_start, y_end, 2):
        for c in range(COLS):
            h = heights[c]
            if y0 % h != 0:
                continue
            x0 = c * 8
            region = err[y0 : y0 + h + 2 * R, x0 : x0 + 8 + 2 * R]
            current = indices[y0 : y0 + h, x0 : x0 + 8]
            es = np.empty((npairs, h + 2 * R, 8 + 2 * R, 3))
            colss = np.empty((npairs, h, 8), dtype=np.int64)
            totals = np.zeros(npairs)
            for p in prange(npairs):
                es[p] = region
                colss[p] = current
                totals[p] = _trial(es[p], colss[p], pair_arr[p, 0], pair_arr[p, 1], h, y0, x0, wpad, s2, pal, G)
            best_p = int(np.argmin(totals))
            if totals[best_p] < -1e-9:
                a, b = pair_arr[best_p, 0], pair_arr[best_p, 1]
                if a != pairs[y0 // 2, c, 0] or b != pairs[y0 // 2, c, 1]:
                    changed += 1
                for r in range(h // 2):
                    pairs[y0 // 2 + r, c, 0] = a
                    pairs[y0 // 2 + r, c, 1] = b
                indices[y0 : y0 + h, x0 : x0 + 8] = colss[best_p]
                err[y0 : y0 + h + 2 * R, x0 : x0 + 8 + 2 * R] = es[best_p]
    return changed


@njit(cache=True)
def flip_pixels(indices, pairs, err, wpad, s2, pal, G):
    """Each pixel tries its cell's other colour. Returns pixels changed."""
    changed = 0
    for y in range(H):
        for x in range(W):
            a, b = pairs[y // 2, x // 8, 0], pairs[y // 2, x // 8, 1]
            if a == b:
                continue
            cur = indices[y, x]
            other = b if cur == a else a
            v0, v1, v2 = _gradient(err, y, x, wpad, G, y, x)
            d0, d1, d2 = pal[other, 0] - pal[cur, 0], pal[other, 1] - pal[cur, 1], pal[other, 2] - pal[cur, 2]
            if 2 * (d0 * v0 + d1 * v1 + d2 * v2) + (d0 * d0 + d1 * d1 + d2 * d2) * s2[y, x] < -1e-12:
                indices[y, x] = other
                _apply(err, y, x, G, d0, d1, d2)
                changed += 1
    return changed
