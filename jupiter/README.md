# Jupiter on the ZX Spectrum

A conversion of the Hubble Space Telescope's June 2019 portrait of Jupiter
(NASA, ESA, A. Simon and M. H. Wong; public domain; `source/hubble2019.jpg`).

| File | What it is |
|---|---|
| `jupiter.tap` | 128K / +2 version with multicolour: colour cells 8x2 pixels instead of 8x8 |
| `jupiter48.tap`, `jupiter48.scr` | plain screen for any Spectrum |
| `build/*_blended.png` | how each version looks with neighbouring pixels blended, as the eye sees it |
| `build/*_emulator.png` | screenshots taken from Fuse |

Load with `..\.venv\Scripts\specbot.exe load jupiter\jupiter.tap` from the
project folder, or open the tape in any emulator set to a 128K machine.

## The tricks

**In the conversion (`convert.py`)**

- Everything is done in linear light, so dithered tones come out at the
  brightness of the photograph rather than too dark.
- Only black, red, yellow, white and cyan are used (in both brightnesses).
  Jupiter's creams, tans and rusts are all mixes of those; the other Spectrum
  colours would average out correctly but look wrong close up.
- The planet's limb, which fades to black in the photograph, is lifted and cut
  off cleanly, and any cell touching the sky must use black, so the edge of
  the disc is sharp instead of a scatter of dots.
- Each cell picks the ink/paper pair whose *blends* best match its pixels, and
  rows of cells are chosen one after another with the error left by the row
  above included. Neighbouring rows therefore complement each other: one
  offers white and yellow, the next yellow and red, and the eye sees tan.
- A final direct-search pass compares the blurred Spectrum picture with the
  blurred photograph (a simple model of the eye) and keeps trying other colour
  pairs for each cell, and the other colour for each pixel, while that
  improves. This cut the measured error by about a third over error diffusion.

**In the display (`multicolour.py`)**

- The 128K version rewrites 2,304 attribute bytes every frame in step with the
  TV beam, giving 8x2 colour cells over the 24 columns the planet occupies.
  Jupiter's bands are horizontal, so finer vertical colour resolution is
  exactly where it helps.
- The routine is about 9 KB of generated straight-line code. The generator
  simulates the Z80 T-state by T-state, including the 0-6 T-state delays the
  video chip adds to screen-memory writes, places each write between the beam
  reading that byte for one pair of lines and the next, and checks the margin
  (13 T-states after the beam, 23 before it comes round again).
- It synchronises itself: each block's first write is timed to arrive early in
  a contention cycle, so the video chip always holds it to the same instant.

## Not used, and why

- **Gigascreen** (two screens alternated every frame to blend more colours):
  flickers at 25 Hz in an emulator without frame blending, and a screenshot
  only ever shows one of the two frames.
- **Border effects**: space is black; there is nothing accurate to draw there.
- **8x1 multicolour**: possible only across about 16 columns, which would mean
  a planet two-thirds the size. A bigger disc with 8x2 cells shows more.

## Rebuilding

    ..\.venv\Scripts\python.exe build.py            # uses the cached conversion
    ..\.venv\Scripts\python.exe build.py --fresh    # redo the conversion (several minutes)
    ..\.venv\Scripts\python.exe build.py --verify   # load in Fuse and compare every pixel

`--verify` captures the emulator screen and checks it is identical to the
picture the converter intended, which is also the test that the multicolour
timing is right.
