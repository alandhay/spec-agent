"""Generate the multicolour display routine for the 128K Spectrum.

The Spectrum's colour attributes normally cover 8x8 pixels. The video chip
re-reads them on every scan line, though, so a program that rewrites them in
step with the TV beam gets smaller colour cells. This routine gives 8x2 cells
across 24 columns (192 pixels) for all 192 lines: 2,304 attribute bytes
rewritten every frame, each landing in the 228 T-state gap between the beam
reading it for one pair of lines and needing it for the next.

There is no time for loops or tables, so the routine is straight-line code
generated here: for each pair of lines, eight register pairs are loaded with
attribute values, then stored left to right chasing the beam, then four more
are loaded and stored. Writes to screen memory are slowed by the video chip
by 0-6 T-states depending on the exact moment ("contention"), so the generator
simulates the Z80 T-state by T-state to place every instruction, and pads each
block so the next one starts exactly two scan lines later.

Timings are for the 128K / +2 (228 T-states per line, 311 lines). It will not
show correctly on a 48K machine, whose lines are 224 T-states long.
"""

from __future__ import annotations

import numpy as np

ORG = 0x8000
IM2_TABLE = 0xBE00                  # 257 bytes of 0xBF -> interrupt vector 0xBFBF
IM2_HANDLER = 0xBFBF
STACK = 0xBDF0

T_LINE0 = 14362                     # T-state (after the interrupt) of the first display pixel
T_PER_LINE = 228
CONTENTION_START = T_LINE0 - 1
CONTENTION = (6, 5, 4, 3, 2, 1, 0, 0)

MC_COL0, MC_COLS = 4, 24
PAIR_ROWS = 96
LEAD = 29                           # first write of a block lands this long after the beam passes the left edge


def contention_delay(t: int) -> int:
    rel = t - CONTENTION_START
    if rel < 0 or rel >= 192 * T_PER_LINE:
        return 0
    pos = rel % T_PER_LINE
    return CONTENTION[pos % 8] if pos < 128 else 0


class Emitter:
    """Appends Z80 instructions and tracks the T-state each one runs at."""

    def __init__(self) -> None:
        self.code = bytearray()
        self.t = 0
        self.writes: list[tuple[int, int]] = []     # (address, T-state of the write)

    def raw(self, *data: int, t: int = 0) -> None:
        self.code += bytes(data)
        self.t += t

    def load(self, reg: str, value: int) -> None:
        lo, hi = value & 0xFF, value >> 8
        op, t = {"bc": ((0x01,), 10), "de": ((0x11,), 10), "hl": ((0x21,), 10),
                 "ix": ((0xDD, 0x21), 14), "iy": ((0xFD, 0x21), 14)}[reg]
        self.raw(*op, lo, hi, t=t)

    def store(self, reg: str, addr: int) -> None:
        """ld (addr),reg: two bytes written to (contended) screen memory."""
        op = {"hl": (0x22,), "bc": (0xED, 0x43), "de": (0xED, 0x53),
              "ix": (0xDD, 0x22), "iy": (0xFD, 0x22)}[reg]
        self.raw(*op, addr & 0xFF, addr >> 8, t=4 * len(op) + 6)    # opcode fetch(es) + address bytes
        for a in (addr, addr + 1):
            self.t += contention_delay(self.t)
            self.writes.append((a, self.t))
            self.t += 3

    def exx(self) -> None:
        self.raw(0xD9, t=4)

    def delay(self, n: int) -> None:
        """Burn exactly n T-states (uses A and B)."""
        if n < 0:
            raise ValueError(f"cannot delay {n} T-states")
        if n >= 60:
            loops = (n - 22) // 13
            loops = min(loops, 256)
            self.raw(0x06, loops & 0xFF, 0x10, 0xFE, t=13 * loops + 2)     # ld b,n : djnz $
            self.delay(n - (13 * loops + 2))
            return
        for sevens in range(n // 7 + 1):
            rest = n - 7 * sevens
            if rest % 4 == 0:
                for _ in range(sevens):
                    self.raw(0x3E, 0x00, t=7)                               # ld a,0
                for _ in range(rest // 4):
                    self.raw(0x00, t=4)                                     # nop
                return
        raise ValueError(f"cannot delay exactly {n} T-states")

    def delay_until(self, t: int) -> None:
        self.delay(t - self.t)


LOAD_ORDER = ("hl", "bc", "de", None, "hl", "bc", "de", None, "ix", "iy")   # None = exx
LOAD_T = 10 * 6 + 4 * 2 + 14 * 2


def generate(attrs: np.ndarray) -> tuple[bytes, dict]:
    """Build the machine code for a [96, 32] array of attributes (one row per pair of lines)."""
    e = Emitter()
    # -- set-up: own stack, interrupt mode 2 with a handler that just returns ---------
    e.raw(0xF3)                                             # di
    e.raw(0x31, STACK & 0xFF, STACK >> 8)                   # ld sp,STACK
    e.raw(0x21, 0x00, IM2_TABLE >> 8, 0x11, 0x01, IM2_TABLE >> 8, 0x01, 0x00, 0x01)
    e.raw(0x36, IM2_HANDLER >> 8, 0xED, 0xB0)               # ld (hl),0xBF : ldir
    e.raw(0x3E, 0xC9, 0x32, IM2_HANDLER & 0xFF, IM2_HANDLER >> 8)   # handler = ret
    e.raw(0x3E, IM2_TABLE >> 8, 0xED, 0x47, 0xED, 0x5E)     # ld i,a : im 2
    e.raw(0xAF, 0xD3, 0xFE)                                 # black border
    frame = ORG + len(e.code)
    e.raw(0xFB, 0x76)                                       # ei : halt
    # The interrupt arrives at T=0: 19 T-states to accept it, 10 for the handler's ret.
    e.t = 29

    def read_time(line: int, col: int) -> int:
        return T_LINE0 + line * T_PER_LINE + 4 * col

    block_of_write = []
    for k in range(-1, PAIR_ROWS - 1):                      # block k sets up pair row k + 1
        pair = k + 1
        values = attrs[pair, MC_COL0 : MC_COL0 + MC_COLS].astype(int)
        words = [int(values[i]) | int(values[i + 1]) << 8 for i in range(0, MC_COLS, 2)]
        base = 0x5800 + (pair // 4) * 32 + MC_COL0
        # The first store's write should *arrive* one T-state into a contention cycle, so it
        # is always held to the same moment: that absorbs any small jitter in the interrupt.
        arrive = T_LINE0 - 1 + (2 * k + 1) * T_PER_LINE + (LEAD - 5) + 1
        e.delay_until(arrive - 10 - LOAD_T)
        it = iter(words[:8])
        for reg in LOAD_ORDER:
            e.exx() if reg is None else e.load(reg, next(it))
        n = 0
        for reg in ("hl", "bc", "de", None, "hl", "bc", "de", "ix", "iy"):
            if reg is None:
                e.exx()
            else:
                e.store(reg, base + 2 * n)
                n += 1
        for word in words[8:]:
            e.load("hl", word)
            e.store("hl", base + 2 * n)
            n += 1
        block_of_write += [k] * MC_COLS

    # Back to the halt. Finish on a multiple of 4 T-states so the interrupt is always
    # accepted at the same moment (a halted Z80 only notices it every fourth T-state).
    e.delay(20 + (-(e.t + 20 + 10 + 4 + 4)) % 4)
    e.raw(0xC3, frame & 0xFF, frame >> 8, t=10)             # jp frame
    end_t = e.t + 8

    # -- verify every write lands inside its window ------------------------------------
    worst_after, worst_before = 10**9, 10**9
    for (addr, t), k in zip(e.writes, block_of_write):
        col = (addr - 0x5800) % 32
        before = read_time(2 * k + 2, col) - t              # must be written before the next read
        after = t - read_time(2 * k + 1, col) if k >= 0 else 10**9   # ... and after the previous one
        worst_after, worst_before = min(worst_after, after), min(worst_before, before)
    info = {
        "bytes": len(e.code),
        "slack_after_beam": worst_after,
        "slack_before_beam": worst_before,
        "frame_T": end_t,
        "end_mod_4": end_t % 4,
    }
    if worst_after < 6 or worst_before < 6:
        raise SystemExit(f"timing does not fit: {info}")
    if ORG + len(e.code) > STACK - 64:
        raise SystemExit("generated code is too long")
    if end_t >= 311 * T_PER_LINE:
        raise SystemExit("routine overruns the frame")
    return bytes(e.code), info
