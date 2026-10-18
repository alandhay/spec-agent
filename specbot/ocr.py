"""Read the text on a Spectrum screen by matching 8x8 cells against the ROM font."""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from .errors import CaptureError, ConfigError

COLUMNS, ROWS = 32, 24
PAPER_W, PAPER_H = COLUMNS * 8, ROWS * 8
_FONT_OFFSET = 0x3D00  # character set in the 48K BASIC ROM, codes 32-127
_SPECIAL = {0x60: "£", 0x7F: "©"}
UNKNOWN = "?"


def load_font(rom_path: str | Path) -> dict[bytes, str]:
    """Map each character's 8 bitmap bytes to the character, from a Spectrum ROM file."""
    try:
        data = Path(rom_path).read_bytes()
    except OSError as exc:
        raise ConfigError(f"cannot read ROM file {rom_path}: {exc}") from None
    if len(data) < 16384 or len(data) % 16384:
        raise ConfigError(f"{rom_path} is not a Spectrum ROM image (size {len(data)})")
    rom = data[-16384:]  # in multi-ROM images the 48K BASIC ROM comes last
    font = {}
    for code in range(32, 128):
        glyph = rom[_FONT_OFFSET + (code - 32) * 8 : _FONT_OFFSET + (code - 31) * 8]
        font.setdefault(glyph, _SPECIAL.get(code, chr(code)))
    return font


def read_screen_text(img: Image.Image, font: dict[bytes, str]) -> list[str]:
    """Return the 24 text rows of a native-resolution screenshot.

    ``img`` must be at 1 image pixel per Spectrum pixel, either the 256x192
    paper alone or with an equal border on each side (e.g. 320x240). Cells
    that are not a ROM character (graphics, UDGs) come back as ``?``.
    """
    width, height = img.size
    if width < PAPER_W or height < PAPER_H or (width - PAPER_W) % 2 or (height - PAPER_H) % 2:
        raise CaptureError(
            f"cannot read text from a {width}x{height} image: set native_size so the "
            "screenshot is at Spectrum resolution (e.g. [320, 240] or [256, 192])"
        )
    left, top = (width - PAPER_W) // 2, (height - PAPER_H) // 2
    img = img.convert("RGB")
    lines = []
    for row in range(ROWS):
        chars = []
        for col in range(COLUMNS):
            x, y = left + col * 8, top + row * 8
            cell = img.crop((x, y, x + 8, y + 8))
            colours = cell.getcolors(2)
            if colours is None:
                chars.append(UNKNOWN)
                continue
            if len(colours) == 1:
                chars.append(" ")
                continue
            ink = bytes(colours[0][1])
            raw = cell.tobytes()
            pixels = [raw[i : i + 3] for i in range(0, len(raw), 3)]
            glyph = bytes(
                sum(0x80 >> bit for bit in range(8) if pixels[line * 8 + bit] == ink)
                for line in range(8)
            )
            inverse = bytes(b ^ 0xFF for b in glyph)
            chars.append(font.get(glyph) or font.get(inverse) or UNKNOWN)
        lines.append("".join(chars).rstrip())
    return lines
