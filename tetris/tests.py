"""Scripted tests for Tetris, run on the real emulator through specbot.

Each test builds the game with TEST defined: the title screen is skipped, the
pieces come from a fixed list and the "keys" come from a script, so no real
key presses are sent and the emulator window never needs the focus. When the
script ends the game returns to BASIC, which prints two frame counters, and
the test reads the result off the screen.

    python tests.py            run every test
    python tests.py NAME ...   run some
    python tests.py --look     just decode whatever is on the emulator screen

Script tokens are keys + a frame count: L/R move, T rotate, C rotate back,
S soft drop, D hard drop, . nothing. "L1 .1 D1 .30" = tap left, tap drop, wait.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import specbot  # noqa: E402
from specbot.ocr import load_font  # noqa: E402

WELL_X, WELL_Y = 11, 2
TAP = ".1 "


def taps(keys: str) -> str:
    """'LLD' -> 'L1 .1 L1 .1 D1 .1 ' (each key tapped for one frame)."""
    return "".join(f"{k}1 .1 " for k in keys)


TESTS = {
    # Two flat I pieces and two upright ones complete the bottom row.
    "line": dict(
        pieces="IIIIIIII",
        script=taps("LLLD") + ".2 " + taps("RD") + ".2 " + taps("TRRRD") + ".2 " + taps("TRRRRD") + ".40",
        score="000176", lines="0001", level="01",
        bottom=["........II", "........II", "........II"],
    ),
    # Eight flat I pieces then two upright ones: four rows at once.
    "tetris": dict(
        pieces="IIIIIIIIIIIIII",
        script=(taps("LLLD") + ".2 " + taps("RD") + ".2 ") * 4 + taps("TRRRD") + ".2 " + taps("TRRRRD") + ".40",
        score="001528", lines="0004", level="01",
        bottom=[],
    ),
    # Holding left auto-repeats to the wall; an upright I against the wall kicks out when rotated.
    # (The piece falls one row by itself during the hold, so the drop is 16 rows.)
    "kick": dict(
        pieces="IIII",
        script="T1 .1 L40 .1 T1 .1 D1 .40",
        score="000032", lines="0000", level="01",
        bottom=["IIII......"],
    ),
    # Holding soft drop takes the piece down at 2 frames a row and scores 1 per row.
    "soft": dict(
        pieces="TTTT",
        script="S70 .30",
        score="000018", lines="0000", level="01",
        bottom=["....T.....", "...TTT...."],
    ),
    # Every piece: rotate each a different amount, spread them out, no clears.
    "shapes": dict(
        pieces="OTSZJLIOT",
        script=taps("LLLLD") + ".2 " + taps("TLD") + ".2 " + taps("RRD") + ".2 " + taps("TRRRRRD") + ".2 "
        + taps("CLLLD") + ".2 " + taps("TTRD") + ".2 " + taps("TD") + ".40",
        score=None, lines="0000", level="01",
        bottom=None,
    ),
    # Ten lines takes the game to level 2.
    "level": dict(
        pieces="I" * 30,
        script=((taps("LLLD") + ".2 " + taps("RD") + ".2 ") * 4 + taps("TRRRD") + ".2 " + taps("TRRRRD") + ".2 ") * 3
        + ".40",
        score=None, lines="0012", level="02",
        bottom=[],
    ),
    # Dropping pieces straight down the middle ends the game.
    "gameover": dict(
        pieces="O" * 14,
        script=(taps("D") + ".2 ") * 12 + ".200",
        score="000180", lines="0000", level="01",
        bottom=None, over=True,
    ),
}


def classify(rgb: tuple[int, int, int]) -> str:
    """Spectrum colour of a pixel as one of the piece letters, 'x' (grey) or '.' (black)."""
    r, g, b = (c > 100 for c in rgb)
    if not (r or g or b):
        return "."
    if r and g and b:
        return "L" if max(rgb) > 235 else "x"
    return {(0, 1, 1): "I", (1, 1, 0): "O", (1, 0, 1): "T", (0, 1, 0): "S", (1, 0, 0): "Z", (0, 0, 1): "J"}[
        (int(r), int(g), int(b))
    ]


def decode(emu) -> dict:
    """Read the game screen: the well as 20 strings, and the text (plain or bold ROM font)."""
    img = emu.screenshot().convert("RGB")
    left, top = (img.width - 256) // 2, (img.height - 192) // 2
    field = []
    for y in range(20):
        row = ""
        for x in range(10):
            px, py = left + (WELL_X + x) * 8, top + (WELL_Y + y) * 8
            paper = classify(img.getpixel((px + 3, py + 3)))
            ink = classify(img.getpixel((px, py)))
            row += paper if paper != "." else ink.lower() if ink != "." else "."
        field.append(row)

    font = load_font(emu.rom_path)
    bold = {bytes(b | (b >> 1) for b in glyph): ch for glyph, ch in font.items()}
    bold.update(font)
    from specbot.ocr import read_screen_text

    return {"field": field, "text": read_screen_text(img, bold), "image": img}


def show(state: dict) -> None:
    for y, row in enumerate(state["field"]):
        print(f"   {y:2d} |{row}|")
    for n, line in enumerate(state["text"]):
        if line.strip():
            print(f"   text {n:2d}: {line}")


def run(name: str, emu_config: str) -> bool:
    import build

    spec = TESTS[name]
    # Keys already down when a game starts are ignored, so begin with a short pause.
    tap = build.build("test_" + name, (spec["pieces"], ".2 " + spec["script"]))
    with specbot.open_emulator(emu_config) as emu:
        emu.load_file(str(tap))
        report = emu.wait_for_report(180)
        state = decode(emu)
    out = HERE / "build" / f"test_{name}.png"
    state["image"].save(out)

    text, field = state["text"], state["field"]
    problems = []
    if report is None:
        problems.append("the test did not finish (no BASIC report)")
    elif not report.startswith("9 STOP"):
        problems.append(f"unexpected report {report!r}")
    counters = re.search(r"h=(\d+) f=(\d+)", text[0])
    if not counters:
        problems.append(f"no frame counters on row 0: {text[0]!r}")
    else:
        halts, frames = map(int, counters.groups())
        # The counters are read a moment apart, so allow one frame either way.
        if abs(frames - halts) > 1:
            problems.append(f"dropped frames: waited for {halts} but {frames} went by")
    for label, row, col, width in (("score", 5, 2, 6), ("lines", 15, 3, 4), ("level", 11, 26, 2)):
        want = spec.get(label)
        got = text[row][col : col + width]
        if want is not None and got != want:
            problems.append(f"{label}: expected {want}, screen shows {got!r}")
    bottom = spec.get("bottom")
    if bottom is not None:
        want = ["." * 10] * (10 - len(bottom)) + bottom
        got = [re.sub('[a-z]', '.', row) for row in field[10:]]  # ignore the ghost piece
        if got != want:
            problems.append("rows 10-19 of the well are not as expected: wanted " + " ".join(want[-4:]))
    if spec.get("over") and "GAME" not in text[10]:
        problems.append("no GAME OVER message")

    print(f"{'PASS' if not problems else 'FAIL'}  {name}  ({text[0].strip()}; score {text[5].strip()}; report {report})")
    for p in problems:
        print("      - " + p)
    if problems or "-v" in sys.argv:
        show(state)
    return not problems


def main() -> None:
    config = str(ROOT / "specbot.toml")
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if "--look" in sys.argv:
        with specbot.open_emulator(config) as emu:
            state = decode(emu)
            state["image"].save(HERE / "build" / "look.png")
            show(state)
        return
    names = args or list(TESTS)
    results = [run(name, config) for name in names]
    print(f"{sum(results)}/{len(results)} passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
