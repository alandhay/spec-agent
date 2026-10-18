"""Abstract Emulator interface shared by all backends, plus the factory."""

from __future__ import annotations

import logging
import os
import re
import time
from abc import ABC, abstractmethod
from typing import Any, Callable, Sequence

from PIL import Image

from .capture import changed_fraction, postprocess
from .config import Config, load_config
from .errors import ConfigError, SpecbotError
from .keys import Chord, KeySpec, resolve_chord, text_to_chords

log = logging.getLogger("specbot")

# A BASIC report in the lower screen, e.g. "0 OK, 170:1" or "2 Variable not found, 80:1".
REPORT_RE = re.compile(r"^[0-9A-Za-z] .+, ?\d+:\d+$")


def _save(img: Image.Image, path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    img.save(path)


# The Spectrum ROM scans the keyboard once per 50 Hz frame.
FRAME_S = 0.02


class Emulator(ABC):
    """Control a ZX Spectrum emulator: see the screen, press keys, wait.

    Backends implement :meth:`_capture_raw` and :meth:`_press_chord`; everything
    else is built on those.
    """

    def __init__(
        self,
        *,
        crop: Sequence[int] = (0, 0, 0, 0),
        native_size: Sequence[int] | None = None,
        hold_frames: int = 3,
        gap_frames: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        rom_path: str | None = None,
    ) -> None:
        self.rom_path = rom_path
        self._font = None
        self.crop = tuple(crop)
        self.native_size = tuple(native_size) if native_size else None
        self.hold_frames = hold_frames
        self.gap_frames = gap_frames
        self._sleep = sleep

    @abstractmethod
    def _capture_raw(self) -> Image.Image:
        """Capture the emulator display before crop/scale post-processing."""

    @abstractmethod
    def _press_chord(self, chord: Chord, hold_frames: int) -> None:
        """Press and release one chord of canonical Spectrum keys."""

    def raw_screenshot(self, path: str | None = None) -> Image.Image:
        """Capture without applying ``crop`` / ``native_size`` (used by calibrate)."""
        img = self._capture_raw()
        if path:
            _save(img, path)
        return img

    def screenshot(self, path: str | None = None) -> Image.Image:
        """Capture the emulator display; optionally save it to ``path``."""
        img = postprocess(self._capture_raw(), self.crop, self.native_size)
        if path:
            _save(img, path)
        return img

    def screen_text(self) -> list[str]:
        """Read the 24 rows of text on screen by matching the ROM character set.

        Needs ``native_size`` set and a ROM image (``rom`` in the config).
        Cells that are not ROM characters (graphics, UDGs) read as ``?``.
        """
        from .ocr import load_font, read_screen_text

        if self._font is None:
            if not self.rom_path:
                raise ConfigError(
                    'reading screen text needs a Spectrum ROM image: set rom = "path/to/48.rom"'
                )
            self._font = load_font(self.rom_path)
        return read_screen_text(self.screenshot(), self._font)

    def wait_for_report(self, timeout_s: float = 30.0, poll_s: float = 0.25) -> str | None:
        """Wait until a BASIC report ("0 OK, 20:1") is on the bottom rows; return it.

        Returns None on timeout: the program is still running, waiting for
        input, or has crashed without a report.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            for line in reversed(self.screen_text()[-2:]):
                if REPORT_RE.match(line.strip()):
                    return line.strip()
            if time.monotonic() >= deadline:
                return None
            self._sleep(poll_s)

    def load_file(self, path: str) -> None:
        """Load a tape or snapshot file (.tap, .tzx, .sna, .z80 ...) into the emulator."""
        raise SpecbotError(f"{type(self).__name__} cannot load files")

    def press(self, *keys: str, hold_frames: int | None = None) -> None:
        """Press keys simultaneously, e.g. press("SYMBOL_SHIFT", "P") for a quote.

        Aliases (``DELETE``, ``BREAK`` ...) and ``"A+B"`` strings are accepted.
        The call returns after the inter-key gap, so back-to-back presses of
        the same key register as separate keystrokes.
        """
        chord = resolve_chord(keys)
        hold = self.hold_frames if hold_frames is None else hold_frames
        log.debug("press %s (hold %d frames)", "+".join(chord), hold)
        self._press_chord(chord, hold)
        self.wait_frames(self.gap_frames)

    def tap_sequence(self, keys: Sequence[KeySpec]) -> None:
        """Press a series of keys/chords one after another."""
        chords = [resolve_chord(k) for k in keys]  # validate before pressing anything
        for chord in chords:
            self.press(*chord)

    def type_text(self, text: str) -> None:
        """Type a literal string, translating characters to Spectrum chords.

        Characters are typed letter by letter, which suits the 128K BASIC
        editor, games and input prompts; 48K keyword-mode entry is not handled.
        """
        self.tap_sequence(text_to_chords(text))

    def wait_frames(self, n: int) -> None:
        """Wait for ``n`` emulated frames (approximated as n * 20 ms of real time)."""
        if n > 0:
            self._sleep(n * FRAME_S)

    def wait_for_change(
        self,
        timeout_s: float = 5.0,
        threshold: float = 0.01,
        *,
        baseline: Image.Image | None = None,
        poll_s: float = 0.1,
    ) -> bool:
        """Poll screenshots until the fraction of changed pixels exceeds threshold.

        The comparison is against ``baseline`` if given, otherwise against a
        screenshot taken when this method is called. Pass a baseline captured
        *before* sending input to avoid missing a change that happens quickly.
        """
        if baseline is None:
            baseline = self.screenshot()
        deadline = time.monotonic() + timeout_s
        while True:
            fraction = changed_fraction(baseline, self.screenshot())
            if fraction > threshold:
                log.debug("screen changed (%.2f%% of pixels)", fraction * 100)
                return True
            if time.monotonic() >= deadline:
                return False
            self._sleep(poll_s)

    def close(self) -> None:
        """Release any held keys and free resources."""

    def __enter__(self) -> "Emulator":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def emulator_from_config(config: Config, load: str | None = None) -> Emulator:
    """Create the backend selected by an already-validated Config.

    With ``load``, the emulator comes up with that tape/snapshot file loaded.
    """
    if config.backend == "zrcp":
        from .zrcp_backend import ZrcpEmulator

        emu = ZrcpEmulator(config)
        if load:
            emu.load_file(load)
        return emu
    from .window_backend import WindowEmulator

    return WindowEmulator(config, load=load)


def open_emulator(config_path: str | None = None, **overrides: Any) -> Emulator:
    """Open an emulator using ``specbot.toml`` (optional) plus keyword overrides.

    Any config field can be overridden, e.g.
    ``open_emulator(backend="zrcp", port=10001)`` or
    ``open_emulator(window_title="Fuse", native_size=(320, 240))``.
    """
    return emulator_from_config(load_config(config_path, **overrides))
