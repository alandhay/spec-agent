# Tetris for the ZX Spectrum

`tetris.tap` is the game: load it in any Spectrum emulator (48K or 128K), or
from this folder's parent run

    .venv\Scripts\specbot.exe load tetris\tetris.tap

## Controls

| Key | Action |
|---|---|
| O / P (or 5 / 8) | move left / right (hold to auto-repeat) |
| Q (or 7) | rotate clockwise |
| W | rotate anticlockwise |
| A (or 6) | soft drop (1 point per row) |
| SPACE (or 0) | hard drop (2 points per row) |
| H | pause |

Lines score 40 / 100 / 300 / 1200 times the level; the level rises every 10
lines and speeds the fall up, to one row per frame at level 20. Pieces come
from a shuffled bag of seven, there is a ghost piece showing where the piece
will land, a next-piece preview, wall kicks, and a half-second lock delay.

## How it works

`src/tetris.asm` is about 3.5 KB of Z80 machine code at 32768, running at a
locked 50 frames per second. The same 8x8 block bitmap is written into every
cell of the well once; after that the game draws only by changing colour
attributes (one byte per cell), straight after the frame interrupt, so there
is no flicker and a whole-well redraw costs a few thousand T-states.

## Building and testing

    ..\.venv\Scripts\python.exe build.py          # -> build\tetris.tap
    ..\.venv\Scripts\python.exe tests.py          # 7 scripted tests on the emulator
    ..\.venv\Scripts\python.exe keytest.py        # short real-keyboard check (takes the focus)

- `build.py` generates the piece tables, assembles with sjasmplus
  (`tools/`, v1.24.0 from github.com/z00m128/sjasmplus) and writes the tape
  with a BASIC loader.
- `tests.py` builds test versions whose pieces and key presses are scripted,
  runs them in Fuse through specbot, and checks the score, lines, level and
  the contents of the well read back from the screen. Each test also checks
  that no frame was dropped (frames waited for = frames elapsed).
