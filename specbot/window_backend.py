"""`window` backend: works with any emulator running in a normal window.

Captures the window's client area from the screen (mss, or PrintWindow) and
injects scan-code key events with SendInput (pydirectinput). Key injection
needs the emulator window to be in the foreground.

The Windows-only modules are imported lazily so this module (and
:class:`ChordPlayer`) can be imported and unit-tested on any OS.
"""

from __future__ import annotations

import atexit
import ctypes
import logging
import os
import string
import subprocess
import sys
import time
from typing import Callable, Protocol

from PIL import Image

from .capture import client_area, is_blank
from .config import Config, LaunchConfig
from .emulator import FRAME_S, Emulator
from .errors import CaptureError, ConfigError, FocusError, SpecbotError, WindowNotFoundError
from .keys import CAPS_SHIFT, ENTER, SPACE, SYMBOL_SHIFT, Chord, split_chord

log = logging.getLogger("specbot")

_IS_WINDOWS = sys.platform == "win32"

LAUNCH_TIMEOUT_S = 10.0
FOCUS_ATTEMPTS = 3
FOCUS_SETTLE_S = 0.05
PW_RENDERFULLCONTENT = 2


def _set_dpi_awareness() -> None:
    """Make this process per-monitor DPI aware so coordinates are real pixels."""
    if not _IS_WINDOWS:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            log.debug("could not set DPI awareness")


_set_dpi_awareness()


def default_keymap() -> dict[str, str]:
    """Default Spectrum key -> host key mapping (suits Fuse and ZEsarUX)."""
    keymap = {c: c.lower() for c in string.ascii_uppercase + string.digits}
    keymap.update({ENTER: "enter", SPACE: "space", CAPS_SHIFT: "shift", SYMBOL_SHIFT: "ctrl"})
    return keymap


class InputDriver(Protocol):
    """Something that can press and release host keys by name."""

    def key_down(self, key: str) -> None: ...
    def key_up(self, key: str) -> None: ...


class PyDirectInputDriver:
    """Scan-code key events via SendInput (pydirectinput)."""

    def __init__(self) -> None:
        import pydirectinput

        # Timing is ours: no built-in pauses, and no fail-safe exception that
        # could stop a key from being released.
        pydirectinput.PAUSE = 0
        pydirectinput.FAILSAFE = False
        self._pdi = pydirectinput

    def validate(self, keys) -> None:
        unknown = sorted({k for k in keys if k not in self._pdi.KEYBOARD_MAPPING})
        if unknown:
            raise ConfigError(f"unknown host key name(s) in keymap: {', '.join(unknown)}")

    def key_down(self, key: str) -> None:
        self._pdi.keyDown(key, _pause=False)

    def key_up(self, key: str) -> None:
        self._pdi.keyUp(key, _pause=False)


class ChordPlayer:
    """Plays chords on an InputDriver with Spectrum-friendly ordering and timing.

    Modifiers go down first, then (one frame later) the main keys; after the
    hold the main keys come up first and the modifiers last. Every key that
    went down is released even if an exception interrupts the chord.
    """

    def __init__(
        self,
        driver: InputDriver,
        keymap: dict[str, str],
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._driver = driver
        self._keymap = keymap
        self._sleep = sleep
        self._down: list[str] = []
        self.used = False

    def _key_down(self, key: str) -> None:
        host = self._keymap[key]
        self.used = True
        self._down.append(host)  # recorded first so a failed press is still released
        self._driver.key_down(host)

    def play(self, chord: Chord, hold_frames: int) -> None:
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
            self.release_held()

    def release_held(self) -> None:
        """Release the keys currently held, most recently pressed first."""
        while self._down:
            host = self._down.pop()
            try:
                self._driver.key_up(host)
            except Exception:  # keep releasing the rest
                log.exception("failed to release host key %r", host)

    def release_all(self) -> None:
        """Release every mapped host key, whether or not we think it is down."""
        self._down.clear()
        for host in dict.fromkeys(self._keymap.values()):
            try:
                self._driver.key_up(host)
            except Exception:
                log.debug("failed to release host key %r", host, exc_info=True)


def _visible_windows() -> list[tuple[int, str, str]]:
    import win32gui

    found: list[tuple[int, str, str]] = []

    def visit(hwnd: int, _: object) -> bool:
        if win32gui.IsWindowVisible(hwnd):
            found.append((hwnd, win32gui.GetWindowText(hwnd), win32gui.GetClassName(hwnd)))
        return True

    win32gui.EnumWindows(visit, None)
    return found


def find_windows(title: str | None, window_class: str | None) -> list[tuple[int, str, str]]:
    """Visible top-level windows matching a title substring and/or class name."""
    matches = []
    for hwnd, text, cls in _visible_windows():
        if title is not None and title.lower() not in text.lower():
            continue
        if window_class is not None and window_class.lower() != cls.lower():
            continue
        matches.append((hwnd, text, cls))
    return matches


def _describe(title: str | None, window_class: str | None) -> str:
    parts = []
    if title is not None:
        parts.append(f"title containing {title!r}")
    if window_class is not None:
        parts.append(f"class {window_class!r}")
    return " and ".join(parts)


def find_window(title: str | None, window_class: str | None) -> int:
    """Return the single matching window handle, or raise WindowNotFoundError."""
    matches = find_windows(title, window_class)
    what = _describe(title, window_class)
    if not matches:
        raise WindowNotFoundError(f"no visible window with {what}")
    if len(matches) > 1:
        listing = "; ".join(f"{text!r} (class {cls})" for _, text, cls in matches)
        raise WindowNotFoundError(
            f"{len(matches)} windows match {what}: {listing}. "
            "Use a more specific window_title or set window_class."
        )
    return matches[0][0]


def launch_process(launch: LaunchConfig, extra_args: list[str] | None = None) -> subprocess.Popen:
    """Start the emulator described by the [launch] config."""
    cmd = [launch.path, *launch.args, *(extra_args or [])]
    log.info("launching %s", " ".join(cmd))
    # Ask for the window to open without taking the keyboard focus.
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 4  # SW_SHOWNOACTIVATE
    try:
        return subprocess.Popen(cmd, startupinfo=startup)
    except OSError as exc:
        raise SpecbotError(f"could not launch {launch.path}: {exc}") from None


def _terminate_window_process(hwnd: int, title: str, expected_exe: str) -> None:
    """End the process owning ``hwnd``, but only if it is the configured emulator."""
    import pywintypes
    import win32api
    import win32con
    import win32event
    import win32process

    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    access = (
        win32con.PROCESS_TERMINATE
        | win32con.PROCESS_QUERY_INFORMATION
        | win32con.PROCESS_VM_READ
        | win32con.SYNCHRONIZE
    )
    try:
        handle = win32api.OpenProcess(access, False, pid)
    except pywintypes.error as exc:
        raise SpecbotError(f"cannot restart the emulator (process {pid}): {exc.strerror}") from None
    try:
        exe = win32process.GetModuleFileNameEx(handle, 0)
        if os.path.basename(exe).lower() != os.path.basename(expected_exe).lower():
            raise SpecbotError(
                f"window {title!r} belongs to {os.path.basename(exe)}, not "
                f"{os.path.basename(expected_exe)}; refusing to close it. "
                "Make window_title / window_class more specific."
            )
        log.info("closing emulator process %d", pid)
        win32api.TerminateProcess(handle, 0)
        win32event.WaitForSingleObject(handle, 5000)
    finally:
        win32api.CloseHandle(handle)


class WindowEmulator(Emulator):
    """Emulator controlled through its window: screen grabs and SendInput keys."""

    def __init__(
        self,
        config: Config,
        *,
        driver: InputDriver | None = None,
        sleep: Callable[[float], None] = time.sleep,
        load: str | None = None,
    ) -> None:
        if not _IS_WINDOWS:
            raise SpecbotError("the window backend only works on Windows")
        cfg = config.window
        if cfg.window_title is None and cfg.window_class is None:
            raise ConfigError(
                "the window backend needs window_title or window_class "
                "(set it in specbot.toml or pass --title)"
            )
        super().__init__(
            crop=cfg.crop,
            native_size=cfg.native_size,
            hold_frames=cfg.hold_frames,
            gap_frames=cfg.gap_frames,
            sleep=sleep,
            rom_path=config.rom_path,
        )
        self._capture_mode = cfg.capture
        self._sct = None

        keymap = {**default_keymap(), **cfg.keymap}
        if driver is None:
            driver = PyDirectInputDriver()
            driver.validate(keymap.values())
        self._player = ChordPlayer(driver, keymap, sleep)
        atexit.register(self._release_at_exit)

        self._target = (cfg.window_title, cfg.window_class)
        self._launch = config.launch
        if load:
            self.hwnd = self._relaunch_with(load)
        else:
            self.hwnd = self._find_or_launch(*self._target, config.launch)
        log.debug("using window %#x", self.hwnd)

    def _find_or_launch(self, title, window_class, launch: LaunchConfig | None) -> int:
        if launch is None or find_windows(title, window_class):
            return find_window(title, window_class)
        launch_process(launch)
        return self._wait_for_window(title, window_class, launch)

    def _wait_for_window(self, title, window_class, launch: LaunchConfig) -> int:
        deadline = time.monotonic() + LAUNCH_TIMEOUT_S
        while time.monotonic() < deadline:
            if find_windows(title, window_class):
                time.sleep(0.5)  # let the window finish appearing
                return find_window(title, window_class)
            time.sleep(0.25)
        raise WindowNotFoundError(
            f"launched {launch.path} but no window with {_describe(title, window_class)} "
            f"appeared within {LAUNCH_TIMEOUT_S:.0f} s"
        )

    def _relaunch_with(self, path: str) -> int:
        """Restart the emulator with ``path`` on its command line; return the new window."""
        if self._launch is None:
            raise ConfigError(
                "loading a file with the window backend restarts the emulator, "
                "which needs a [launch] section in the config"
            )
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            raise SpecbotError(f"file not found: {path}")
        for hwnd, title, _ in find_windows(*self._target):
            _terminate_window_process(hwnd, title, self._launch.path)
        deadline = time.monotonic() + 5.0
        while find_windows(*self._target) and time.monotonic() < deadline:
            time.sleep(0.1)
        launch_process(self._launch, [path])
        return self._wait_for_window(*self._target, self._launch)

    def load_file(self, path: str) -> None:
        """Load a tape/snapshot by restarting the emulator with it (state is lost)."""
        self._player.release_held()
        self.hwnd = self._relaunch_with(path)

    # -- capture ---------------------------------------------------------

    def _check_capturable(self) -> dict[str, int]:
        import win32gui

        if not win32gui.IsWindow(self.hwnd):
            raise CaptureError("the emulator window no longer exists")
        if win32gui.IsIconic(self.hwnd):
            raise CaptureError("the emulator window is minimised; restore it to take a screenshot")
        area = client_area(self.hwnd)
        if area["width"] <= 0 or area["height"] <= 0:
            raise CaptureError("the emulator window has an empty client area")
        return area

    def _capture_raw(self) -> Image.Image:
        area = self._check_capturable()
        if self._capture_mode == "printwindow":
            img = self._grab_printwindow(area)
            if is_blank(img):
                log.warning(
                    'PrintWindow returned a black image; try capture = "mss" '
                    "(the window must then be visible on screen)"
                )
            return img
        if self._is_covered(area):
            log.warning(
                "the emulator window is partly covered by another window, which will show "
                'in the capture; bring it to the front or use capture = "printwindow"'
            )
        return self._grab_mss(area)

    def _is_covered(self, area: dict[str, int]) -> bool:
        """True if another window is on top of the client area at any sampled point."""
        import pywintypes
        import win32con
        import win32gui

        xs = [area["left"] + area["width"] * f // 8 for f in (1, 4, 7)]
        ys = [area["top"] + area["height"] * f // 8 for f in (1, 4, 7)]
        try:
            for x in xs:
                for y in ys:
                    top = win32gui.GetAncestor(win32gui.WindowFromPoint((x, y)), win32con.GA_ROOT)
                    if top != self.hwnd:
                        return True
        except pywintypes.error:
            return False  # e.g. a sample point is off-screen
        return False

    def _grab_mss(self, area: dict[str, int]) -> Image.Image:
        if self._sct is None:
            import mss

            self._sct = (getattr(mss, "MSS", None) or mss.mss)()
        shot = self._sct.grab(area)
        return Image.frombytes("RGB", shot.size, shot.rgb)

    def _grab_printwindow(self, area: dict[str, int]) -> Image.Image:
        import win32gui
        import win32ui
        from ctypes import wintypes

        print_window = ctypes.windll.user32.PrintWindow
        print_window.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
        print_window.restype = wintypes.BOOL

        # PrintWindow renders the whole window; cut the client area out of it.
        win_left, win_top, win_right, win_bottom = win32gui.GetWindowRect(self.hwnd)
        width, height = win_right - win_left, win_bottom - win_top
        hwnd_dc = win32gui.GetWindowDC(self.hwnd)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        mem_dc = mfc_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        try:
            bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
            mem_dc.SelectObject(bitmap)
            if not print_window(self.hwnd, mem_dc.GetSafeHdc(), PW_RENDERFULLCONTENT):
                raise CaptureError('PrintWindow failed; try capture = "mss"')
            info = bitmap.GetInfo()
            img = Image.frombuffer(
                "RGB",
                (info["bmWidth"], info["bmHeight"]),
                bitmap.GetBitmapBits(True),
                "raw",
                "BGRX",
                0,
                1,
            )
        finally:
            win32gui.DeleteObject(bitmap.GetHandle())
            mem_dc.DeleteDC()
            mfc_dc.DeleteDC()
            win32gui.ReleaseDC(self.hwnd, hwnd_dc)
        left, top = area["left"] - win_left, area["top"] - win_top
        return img.crop((left, top, left + area["width"], top + area["height"]))

    # -- keys ------------------------------------------------------------

    def ensure_foreground(self) -> None:
        """Bring the emulator window to the foreground, or raise FocusError."""
        import pywintypes
        import win32api
        import win32con
        import win32gui

        if win32gui.GetForegroundWindow() == self.hwnd:
            return
        if not win32gui.IsWindow(self.hwnd):
            raise FocusError("the emulator window no longer exists")
        if win32gui.IsIconic(self.hwnd):
            win32gui.ShowWindow(self.hwnd, win32con.SW_RESTORE)
        for attempt in range(1, FOCUS_ATTEMPTS + 1):
            # Windows only lets a process take the foreground if it received
            # the last input event; a harmless Alt tap satisfies that.
            win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
            win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
            try:
                win32gui.SetForegroundWindow(self.hwnd)
            except pywintypes.error as exc:
                log.debug("SetForegroundWindow attempt %d failed: %s", attempt, exc)
            self._sleep(FOCUS_SETTLE_S)
            if win32gui.GetForegroundWindow() == self.hwnd:
                return
        raise FocusError(
            f"could not bring the emulator window to the foreground after {FOCUS_ATTEMPTS} attempts"
        )

    def _press_chord(self, chord: Chord, hold_frames: int) -> None:
        # Checked before every chord so keys never land in another application
        # if focus moves while a long string is being typed.
        self.ensure_foreground()
        self._player.play(chord, hold_frames)

    def _release_at_exit(self) -> None:
        if self._player.used:
            self._player.release_all()

    def close(self) -> None:
        self._player.release_held()
        atexit.unregister(self._release_at_exit)
        if self._sct is not None:
            self._sct.close()
            self._sct = None
