"""`zrcp` backend: ZEsarUX remote command protocol (no window focus needed).

Protocol notes
--------------
ZEsarUX was not installed on the machine this was written on, so the commands
below were confirmed by reading the ZEsarUX source (github.com/chernandezba/
zesarux, branch main, October 2026: src/zrcp/remote.c, src/utils.h,
src/utils.c) rather than from a live `help`. Re-check with `help <command>`
against your installed version if anything misbehaves.

* Start ZEsarUX with ``--enable-remoteprotocol`` (port 10000;
  ``--remoteprotocol-port N`` to change).
* On connect the server sends
  ``Welcome to ZEsarUX remote command protocol (ZRCP)\\nWrite help for
  available commands\\n`` and then the prompt ``\\ncommand> `` (it becomes
  ``\\ncommand@cpu-step> `` in step mode). Every response ends with the prompt.
* ``send-keys-event key event [nomenu]`` - press (event != 0) or release
  (event 0) one key, so chords work. ``key`` is a value of ``enum util_teclas``
  (utils.h): plain ASCII for letters (lower case) and digits, and from 128 up
  for special keys: SPACE=128, ENTER=129, SHIFT_L=133 (Spectrum CAPS SHIFT),
  CONTROL_L=138 (Spectrum SYMBOL SHIFT). The enum has gained entries over
  time, so the special values can differ in older versions: override them in
  ``[zrcp.keycodes]`` if a modifier does nothing. Errors come back as text
  starting ``ERROR``.
* ``send-keys-ascii`` / ``send-keys-string time ...`` type characters with a
  fixed delay but cannot hold chords; not used.
* ``save-screen file`` - "Currently bmp, scr and pbm file formats supported";
  no PNG, so we save a BMP to a temp file and load it with Pillow. The file is
  written by the emulator, so it must share a filesystem with this process.
* ``smartload file`` loads a tape or snapshot (used by ``load_file``).
* There is no command to wait for N frames (``get-tstates`` only counts
  within a frame), so ``wait_frames`` sleeps n * 20 ms like the window backend.
"""

from __future__ import annotations

import logging
import os
import re
import socket
import string
import tempfile
import time
from typing import Callable

from PIL import Image

from .config import Config
from .emulator import FRAME_S, Emulator
from .errors import CaptureError, ZrcpError
from .keys import CAPS_SHIFT, ENTER, SPACE, SYMBOL_SHIFT, Chord, split_chord

log = logging.getLogger("specbot")

CONNECT_TIMEOUT_S = 5.0
LAUNCH_TIMEOUT_S = 10.0
SCREEN_FILE_TIMEOUT_S = 3.0
DEFAULT_PORT = 10000

_PROMPT_RE = re.compile(r"\n?command(@cpu-step)?> $")


def default_keycodes() -> dict[str, int]:
    """Default Spectrum key -> ``enum util_teclas`` value (see module docstring)."""
    codes = {c: ord(c.lower()) for c in string.ascii_uppercase + string.digits}
    codes.update({SPACE: 128, ENTER: 129, CAPS_SHIFT: 133, SYMBOL_SHIFT: 138})
    return codes


class ZrcpEmulator(Emulator):
    """ZEsarUX controlled over its remote command protocol."""

    def __init__(self, config: Config, *, sleep: Callable[[float], None] = time.sleep) -> None:
        cfg = config.zrcp
        super().__init__(
            crop=cfg.crop,
            native_size=cfg.native_size,
            hold_frames=cfg.hold_frames,
            gap_frames=cfg.gap_frames,
            sleep=sleep,
            rom_path=config.rom_path,
        )
        self._keycodes = {**default_keycodes(), **cfg.keycodes}
        self._down: list[int] = []
        self._sock: socket.socket | None = None
        self._sock = self._connect(cfg.host, cfg.port, config)
        banner = self._read_until_prompt()
        log.debug("connected to ZRCP at %s:%d: %s", cfg.host, cfg.port, banner.splitlines()[:1])

    def _connect(self, host: str, port: int, config: Config) -> socket.socket:
        try:
            return socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_S)
        except OSError as exc:
            if config.launch is None:
                raise ZrcpError(
                    f"could not connect to ZEsarUX at {host}:{port} ({exc}). "
                    "Start it with --enable-remoteprotocol."
                ) from None
        from .window_backend import launch_process

        extra = ["--enable-remoteprotocol"]
        if port != DEFAULT_PORT:
            extra += ["--remoteprotocol-port", str(port)]
        launch_process(config.launch, [a for a in extra if a not in config.launch.args])
        deadline = time.monotonic() + LAUNCH_TIMEOUT_S
        while True:
            try:
                return socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_S)
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise ZrcpError(
                        f"launched {config.launch.path} but could not connect to "
                        f"{host}:{port} within {LAUNCH_TIMEOUT_S:.0f} s ({exc})"
                    ) from None
                time.sleep(0.25)

    # -- protocol --------------------------------------------------------

    def _read_until_prompt(self) -> str:
        if self._sock is None:
            raise ZrcpError("the ZRCP connection is closed")
        buf = b""
        while True:
            try:
                chunk = self._sock.recv(4096)
            except OSError as exc:
                raise ZrcpError(f"ZRCP connection error: {exc}") from None
            if not chunk:
                raise ZrcpError("ZEsarUX closed the remote protocol connection")
            buf += chunk
            text = buf.decode("latin-1")
            match = _PROMPT_RE.search(text)
            if match:
                return text[: match.start()].strip()

    def _send(self, cmd: str) -> str:
        """Send one command and return its response (without the prompt)."""
        if self._sock is None:
            raise ZrcpError("the ZRCP connection is closed")
        log.debug("zrcp> %s", cmd)
        try:
            self._sock.sendall(cmd.encode("latin-1") + b"\n")
        except OSError as exc:
            raise ZrcpError(f"ZRCP connection error: {exc}") from None
        response = self._read_until_prompt()
        if response.upper().startswith(("ERROR", "UNKNOWN COMMAND")):
            raise ZrcpError(f"ZEsarUX rejected {cmd.split()[0]!r}: {response}")
        return response

    # -- capture ---------------------------------------------------------

    def _capture_raw(self) -> Image.Image:
        fd, path = tempfile.mkstemp(prefix="specbot_", suffix=".bmp")
        os.close(fd)
        os.unlink(path)  # so we can tell when ZEsarUX has written it
        try:
            self._send(f'save-screen "{path}"' if " " in path else f"save-screen {path}")
            return self._load_screen_file(path)
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    def _load_screen_file(self, path: str) -> Image.Image:
        deadline = time.monotonic() + SCREEN_FILE_TIMEOUT_S
        error: Exception | None = None
        while True:
            if os.path.exists(path) and os.path.getsize(path) > 0:
                try:
                    with Image.open(path) as img:
                        return img.convert("RGB")
                except (OSError, SyntaxError) as exc:  # still being written
                    error = exc
            if time.monotonic() >= deadline:
                detail = f": {error}" if error else ""
                raise CaptureError(
                    f"ZEsarUX did not produce a readable screenshot at {path}{detail}. "
                    "It must run on the same machine as specbot."
                )
            time.sleep(0.05)

    def load_file(self, path: str) -> None:
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            raise ZrcpError(f"file not found: {path}")
        self._send(f'smartload "{path}"' if " " in path else f"smartload {path}")

    # -- keys ------------------------------------------------------------

    def _key_event(self, code: int, pressed: bool) -> None:
        self._send(f"send-keys-event {code} {1 if pressed else 0}")

    def _key_down(self, key: str) -> None:
        code = self._keycodes[key]
        self._down.append(code)
        self._key_event(code, True)

    def _release_held(self) -> None:
        while self._down:
            code = self._down.pop()
            try:
                self._key_event(code, False)
            except Exception:  # keep releasing the rest
                log.exception("failed to release ZRCP key %d", code)

    def _press_chord(self, chord: Chord, hold_frames: int) -> None:
        modifiers, mains = split_chord(chord)
        try:
            for key in modifiers:
                self._key_down(key)
            if modifiers:
                self._sleep(FRAME_S)
            for key in mains:
                self._key_down(key)
            self._sleep(hold_frames * FRAME_S)
        finally:
            self._release_held()

    def close(self) -> None:
        if self._sock is None:
            return
        self._release_held()
        try:
            self._sock.sendall(b"quit\n")
        except OSError:
            pass
        self._sock.close()
        self._sock = None
