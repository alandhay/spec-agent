# SpecArt

Turns any image into the ZX Spectrum picture that looks most like it, runs
that picture on the emulator, and measures how close the emulator's screen is
to the original.

## Use it

    ..\.venv\Scripts\python.exe -m specart serve        # then open http://127.0.0.1:8765

Drop an image on the page (or press "Try the Jupiter photo") and watch: the
picture appears and sharpens as the search runs, the error graph falls, then
the emulator's own screen is captured and scored.

From the command line:

    ..\.venv\Scripts\python.exe -m specart convert photo.jpg
    ..\.venv\Scripts\python.exe -m specart convert photo.png --mode standard --fit fill --tone lift

Results (`picture.tap`, `picture.scr`, `target.png`, `render.png`,
`blended.png`, `emulator.png`) go in `photo_spectrum/` next to the image.

| Option | Choices |
|---|---|
| Screen (`--mode`) | `multicolour`: 128K, 8x2 colour cells in the middle 24 columns. `standard`: any Spectrum, 8x8 cells |
| Shape (`--fit`) | `fit` whole image with black bars, `fill` crop to cover, `stretch` |
| Shadows (`--tone`) | `original`, `lift`, `lift-more` (see below) |
| Effort (`--effort`) | `thorough`: three starting points, best kept. `quick`: one |

## What it does

1. **Read** anything Pillow opens: JPEG, PNG, GIF (first frame), BMP, TIFF,
   WebP ...; any size; 1-bit to 16-bit, greyscale, palette, CMYK; transparency
   is flattened onto black and camera rotation is honoured.
2. **Fit** to 256x192 in linear light.
3. **Search.** Each cell may use two of the 15 colours. A first picture is
   made by error diffusion, each row of cells choosing its pair with the error
   left by the rows above. Then a direct search tries every colour pair in
   every cell and the other colour for every pixel, keeping whatever brings
   the picture, as the eye would see it, closer to the original.
4. **Build** a tape; the multicolour version includes the beam-synchronised
   display routine (`multicolour.py`).
5. **Run** it in Fuse through specbot and capture the screen.
6. **Measure.** The score is taken from the emulator's screen, and the screen
   is also compared pixel for pixel with what the converter intended, which
   proves the display routine's timing.

## The score

*Perceived error* is the RMS difference in the Oklab colour space between the
two pictures after blurring both at a fine scale (which shows grain and lost
detail) and a coarse one (which shows wrong tone or colour). 0 is identical;
black against white is 1. The page shows `100 x (1 - error)` as a "match".
It is a model of the eye, not a law: use it to compare conversions of the same
image, not to compare different images.

## Dark pictures

The Spectrum has no dark colours: the dimmest thing after black is a blue at
three-quarters brightness. A deep shadow can only be black or a scatter of
bright dots. Measured against the original, leaving shadows as they are always
scores best, so that is the default; but a dark portrait can lose its face.
"Lift shadows" brightens dark tones before the search so there is something to
draw them with. The score is still measured against the original, so it will
be worse; judge that one by eye.

## Limits

- Multicolour needs a 128K or +2 (its timing is 228 T-states per line); the
  outer four columns each side stay 8x8.
- One picture at a time: there is one emulator, and it is restarted for each.
- The server listens on 127.0.0.1 only and keeps jobs in `specart/jobs/`.
