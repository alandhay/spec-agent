"""`x11` backend: a private, headless emulator on Linux.

Each X11Emulator owns its own virtual display (Xvfb) with one emulator window
on it, so several can run side by side on a server and none of them needs a
desktop or the keyboard focus. The screen is read straight from the display
and keys are injected with the XTEST extension (python-xlib).

Like the window backend, loading a file restarts the emulator with the file on
its command line. This backend is built directly rather than from specbot.toml.
"""

from __future__ import annotations

import logging
import os
import queue
import string
import subprocess
import tempfile
import threading
import time
from typing import Callable, Sequence

from PIL import Image

from .config import LaunchConfig
from .emulator import Emulator
from .errors import CaptureError, SpecbotError
from .keys import CAPS_SHIFT, ENTER, SPACE, SYMBOL_SHIFT, Chord, normalize_key
from .window_backend import ChordPlayer

log = logging.getLogger("specbot")

START_TIMEOUT_S = 15.0


def default_keymap() -> dict[str, str]:
    """Default Spectrum key -> X keysym name mapping (suits Fuse)."""
    keymap = {c: c.lower() for c in string.ascii_uppercase + string.digits}
    keymap.update({ENTER: "Return", SPACE: "space", CAPS_SHIFT: "Shift_L", SYMBOL_SHIFT: "Control_L"})
    return keymap


class XTestDriver:
    """Key events on one X display through the XTEST extension."""

    def __init__(self, display, lock: threading.Lock) -> None:
        from Xlib import XK, X
        from Xlib.ext import xtest

        self._display, self._lock, self._xk, self._x, self._xtest = display, lock, XK, X, xtest

    def _send(self, kind: int, key: str) -> None:
        with self._lock:
            code = self._display.keysym_to_keycode(self._xk.string_to_keysym(key))
            if not code:
                raise SpecbotError(f"the X display has no key for {key!r}")
            self._xtest.fake_input(self._display, kind, code)
            self._display.sync()

    def key_down(self, key: str) -> None:
        self._send(self._x.KeyPress, key)

    def key_up(self, key: str) -> None:
        self._send(self._x.KeyRelease, key)


class AudioTap:
    """The emulator's sound, for whoever is listening.

    SDL's "disk" audio driver writes what would be played to a file (it refuses a pipe).
    A thread follows the file as it grows and hands the new bytes to any listeners. The
    file is cut back to nothing now and then: the emulator keeps writing at its own
    position, which leaves a hole rather than using disk space.
    """

    TRIM_BYTES = 8 * 1024 * 1024

    def __init__(self) -> None:
        self._folder = tempfile.mkdtemp(prefix="specbot-audio-")
        self.path = os.path.join(self._folder, "audio")
        open(self.path, "wb").close()
        self._listeners: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._closed = False
        threading.Thread(target=self._follow, daemon=True, name="specbot-audio").start()

    def _follow(self) -> None:
        position = since_trim = 0
        try:
            with open(self.path, "rb", buffering=0) as audio:
                while not self._closed:
                    size = os.fstat(audio.fileno()).st_size
                    if size < position:                 # the emulator was restarted and began the file again
                        position = since_trim = 0
                    if size - position < 2048:
                        time.sleep(0.02)
                        continue
                    audio.seek(position)
                    chunk = audio.read(min(size - position, 16384) & ~3)    # whole stereo samples
                    position += len(chunk)
                    since_trim += len(chunk)
                    if since_trim > self.TRIM_BYTES and position == size:
                        os.truncate(self.path, 0)
                        since_trim = 0
                    with self._lock:
                        for listener in self._listeners:
                            try:
                                listener.put_nowait(chunk)
                            except queue.Full:
                                pass                    # a slow listener misses some sound
        except OSError:
            pass

    def listen(self) -> queue.Queue:
        listener: queue.Queue = queue.Queue(maxsize=64)
        with self._lock:
            self._listeners.append(listener)
        return listener

    def leave(self, listener: queue.Queue) -> None:
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def close(self) -> None:
        self._closed = True
        try:
            os.unlink(self.path)
            os.rmdir(self._folder)
        except OSError:
            pass


class X11Emulator(Emulator):
    """An emulator on its own virtual X display."""

    def __init__(
        self,
        launch: LaunchConfig,
        *,
        rom_path: str | None = None,
        size: Sequence[int] = (320, 240),
        hold_frames: int = 3,
        gap_frames: int = 3,
        keymap: dict[str, str] | None = None,
        load: str | None = None,
        sound: bool = False,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(native_size=size, hold_frames=hold_frames, gap_frames=gap_frames, sleep=sleep,
                         rom_path=rom_path)
        self._launch = launch
        self._size = tuple(size)
        self._lock = threading.Lock()           # one X request at a time; captures come from several threads
        self._xvfb: subprocess.Popen | None = None
        self._proc: subprocess.Popen | None = None
        self._display = None
        self.audio = AudioTap() if sound else None      # raw samples as the emulator produces them
        try:
            self._name = self._start_display()
            from Xlib import display as xdisplay

            self._display = xdisplay.Display(self._name)
            self._keymap = {**default_keymap(), **(keymap or {})}
            self._driver = XTestDriver(self._display, self._lock)
            self._player = ChordPlayer(self._driver, self._keymap, sleep)
            self._start_emulator(load)
        except BaseException:
            self.close()
            raise

    # -- processes ---------------------------------------------------------

    def _start_display(self) -> str:
        """Start Xvfb on a display number of its own choosing; return e.g. ':101'."""
        read_fd, write_fd = os.pipe()
        width, height = self._size
        try:
            self._xvfb = subprocess.Popen(
                ["Xvfb", "-displayfd", str(write_fd), "-screen", "0", f"{width}x{height}x24", "-nolisten", "tcp"],
                pass_fds=[write_fd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            os.close(read_fd)
            raise SpecbotError(f"could not start Xvfb: {exc}") from None
        finally:
            os.close(write_fd)
        number = b""
        with os.fdopen(read_fd, "rb") as pipe:      # Xvfb writes the number, then a newline, when it is ready
            number = pipe.readline().strip()
        if not number.isdigit():
            raise SpecbotError("Xvfb did not start")
        return f":{number.decode()}"

    def _start_emulator(self, load: str | None) -> None:
        cmd = [self._launch.path, *self._launch.args]
        if load:
            load = os.path.abspath(load)
            if not os.path.isfile(load):
                raise SpecbotError(f"file not found: {load}")
            cmd.append(load)
        env = {**os.environ, "DISPLAY": self._name, "SDL_VIDEODRIVER": "x11", "SDL_AUDIODRIVER": "dummy"}
        if self.audio:
            env.update(SDL_AUDIODRIVER="disk", SDL_DISKAUDIOFILE=self.audio.path)
        log.info("launching %s on %s", " ".join(cmd), self._name)
        try:
            self._proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            raise SpecbotError(f"could not launch {self._launch.path}: {exc}") from None
        deadline = time.monotonic() + START_TIMEOUT_S
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise SpecbotError(f"{self._launch.path} exited as soon as it started (code {self._proc.returncode})")
            if self._window() is not None:
                time.sleep(0.2)                 # let it draw its first frame
                return
            time.sleep(0.05)
        raise SpecbotError(f"{self._launch.path} did not open a window within {START_TIMEOUT_S:.0f} s")

    def _window(self):
        """The emulator's window (the only one on this display), or None."""
        from Xlib import X

        with self._lock:
            for child in self._display.screen().root.query_tree().children:
                if child.get_attributes().map_state == X.IsViewable:
                    return child
        return None

    def _stop_emulator(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)

    def load_file(self, path: str) -> None:
        """Load a tape/snapshot by restarting the emulator with it (state is lost)."""
        self._player.release_held()
        self._stop_emulator()
        self._start_emulator(path)

    # -- screen and keys ---------------------------------------------------

    def _capture_raw(self) -> Image.Image:
        from Xlib import X

        if self._proc is None or self._proc.poll() is not None:
            raise CaptureError("the emulator is not running")
        width, height = self._size
        with self._lock:
            raw = self._display.screen().root.get_image(0, 0, width, height, X.ZPixmap, 0xFFFFFFFF)
        return Image.frombytes("RGB", (width, height), raw.data, "raw", "BGRX")

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None and self._proc.poll() is None else None

    def key_event(self, keys: Sequence[str], down: bool) -> None:
        """Hold or let go of Spectrum keys, for a person playing live (no timing of our own)."""
        hosts = [self._keymap[normalize_key(key)] for key in keys]
        if down:
            self._focus()
        for host in hosts if down else reversed(hosts):
            (self._driver.key_down if down else self._driver.key_up)(host)

    def _press_chord(self, chord: Chord, hold_frames: int) -> None:
        self._focus()
        self._player.play(chord, hold_frames)

    def _focus(self) -> None:
        from Xlib import X

        window = self._window()
        if window is not None:                  # no window manager here: give it the keyboard ourselves
            with self._lock:
                self._display.set_input_focus(window, X.RevertToParent, X.CurrentTime)
                self._display.sync()

    def close(self) -> None:
        self._stop_emulator()
        display, self._display = self._display, None
        if display is not None:
            try:
                display.close()
            except Exception:
                pass
        xvfb, self._xvfb = self._xvfb, None
        if xvfb is not None and xvfb.poll() is None:
            xvfb.kill()
            xvfb.wait(timeout=5)
        if self.audio:
            self.audio.close()
