"""The workbench: everything the agent (and the web page) can do to the emulator.

One emulator, one lock. Every method is blocking and returns plain text (or an
image) ready to hand back to the agent; problems come back as SpecbotError or
BenchError with a message the agent can act on.
"""

from __future__ import annotations

import io
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from PIL import Image

from specbot import window_backend
from specbot.config import LaunchConfig, load_config
from specbot.emulator import Emulator, emulator_from_config
from specbot.errors import SpecbotError
from specbot.tape import basic_to_tap

from . import debugger, sandbox

ROOT = Path(__file__).resolve().parent.parent
PROGRAMS = ROOT / "programs"
CONFIG = ROOT / "specbot.toml"
WINDOWS = sys.platform == "win32"
SJASMPLUS = (ROOT / "tetris" / "tools" / "sjasmplus-1.24.0.win" / "sjasmplus.exe" if WINDOWS
             else Path(shutil.which("sjasmplus") or "sjasmplus"))
# Elsewhere each workbench gets a headless Fuse of its own (specbot.x11_backend).
FUSE = os.environ.get("SPECDEV_FUSE", "fuse-sdl")
FUSE_ARGS = shlex.split(os.environ.get("SPECDEV_FUSE_ARGS", "--machine 128"))
# What that Fuse writes through SDL here (measured): 16-bit signed, little-endian, both channels the same.
SOUND_FROM = {"rate": 48000, "channels": 2}
ROM = os.environ.get("SPECDEV_ROM", "/usr/share/spectrum-roms/48.rom")
# Fuse can take a while to show its window on a busy machine.
window_backend.LAUNCH_TIMEOUT_S = 30.0

LOADABLE = (".tap", ".tzx", ".sna", ".z80")
NO_REPORT = "(no BASIC report: the program is still running, waiting for input, or has crashed)"


class BenchError(Exception):
    """A request the workbench cannot carry out, worded for the agent."""


class Bench:
    def __init__(self, programs: Path = PROGRAMS, home: Path | None = None) -> None:
        self.programs = programs            # this workbench's folder: nothing outside it is touched
        # The person's folder. With the sandbox on (hosted) the emulator and assembler can use only
        # the programs folder and, for the emulator, a scratch folder of its own in here.
        self.home = home or programs.parent
        self._lock = threading.RLock()
        self._emu: Emulator | None = None
        self._last_png: bytes | None = None
        self.busy = ""                      # what the emulator is doing, for the page
        self.last_run: str | None = None    # the program most recently loaded
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        """Give up any wait in progress (the person pressed Stop)."""
        self._cancelled.set()

    def _wait(self, emu: Emulator, seconds: float) -> str | None:
        """Wait for a BASIC report, in short steps so Stop is not held up by a long wait."""
        self._cancelled.clear()
        deadline = time.monotonic() + max(0.0, min(float(seconds), 600.0))
        while True:
            report = emu.wait_for_report(0)
            if report or time.monotonic() >= deadline or self._cancelled.is_set():
                return report
            time.sleep(0.25)

    def path(self, name: str) -> Path:
        """A file inside the programs folder; anything that points outside is refused."""
        root = self.programs.resolve()
        path = (root / name).resolve()
        if root not in path.parents:
            raise BenchError(f"{name!r} is not a file inside the programs folder")
        return path

    # -- the emulator handle -------------------------------------------------

    def _window_exists(self) -> bool:
        if not WINDOWS:
            return False                    # a private emulator: there is none until a program is run
        from specbot.window_backend import find_windows

        cfg = load_config(str(CONFIG))
        return cfg.backend != "window" or bool(find_windows(cfg.window.window_title, cfg.window.window_class))

    def _open(self, load: str | None = None, extra: list[str] | None = None, stdout: str | None = None) -> Emulator:
        """The emulator, started if need be; with ``load`` it is restarted with that file.

        ``extra`` (more arguments for Fuse) and ``stdout`` (a file for its output) are for
        debug runs, on the headless emulator only.
        """
        if self._emu is not None and load is None:
            try:
                self._emu.screenshot()
                return self._emu
            except SpecbotError:
                pass                        # the window went away: open it again
        if not WINDOWS:
            if self._emu is not None and load is not None:
                try:
                    self._emu.load_file(load, extra or (), stdout)
                    return self._emu
                except SpecbotError:
                    pass                    # start again from nothing
            if self._emu is not None:
                self._emu.close()
                self._emu = None
            from specbot.x11_backend import X11Emulator

            scratch = self.home / "emulator"
            scratch.mkdir(parents=True, exist_ok=True)
            launch = sandbox.command([FUSE, *FUSE_ARGS], read=[self.programs, Path(ROM).parent], write=[scratch],
                                     network=False, env={"HOME": str(scratch)})
            self._emu = X11Emulator(LaunchConfig(launch[0], launch[1:]), rom_path=ROM, load=load, sound=True,
                                    scratch=str(scratch), extra_args=extra or (), stdout=stdout)
            return self._emu
        if self._emu is not None:
            self._emu.close()
            self._emu = None
        self._emu = emulator_from_config(load_config(str(CONFIG)), load=load)
        return self._emu

    def close(self) -> None:
        with self._lock:
            if self._emu is not None:
                self._emu.close()
                self._emu = None

    # -- what the agent can do ----------------------------------------------

    def _screen(self, emu: Emulator, report: str | None) -> str:
        rows = emu.screen_text()
        self._remember(emu.screenshot())
        body = "\n".join(f"{i:2d}|{row}" for i, row in enumerate(rows))
        return f"{body}\n\nreport: {report or NO_REPORT}"

    def run(self, file: str, wait_seconds: float = 60, code: list[str] | None = None) -> str:
        """Load a program into a freshly started emulator, wait for its report, return the screen."""
        target = self._loadable(file, code)
        with self._lock:
            self.busy = f"running {file}"
            self.last_run = file
            try:
                emu = self._open(load=str(target))
                report = self._wait(emu, wait_seconds)
                return self._screen(emu, report)
            finally:
                self.busy = ""

    def debug(self, file: str, points: list[debugger.Breakpoint], wait_seconds: float = 30,
              code: list[str] | None = None) -> str:
        """Run a program with breakpoints (Fuse's debugger, scripted); return what each hit showed, then the screen."""
        if WINDOWS:
            raise BenchError("breakpoints need the hosted workbench: Fuse's debugger cannot be scripted on this machine")
        target = self._loadable(file, code)
        labels: dict[str, int] = {}
        syms = [self.path(spec.rpartition("@")[0]).with_suffix(".sym") for spec in code or []]
        for sym in syms or sorted(self.programs.glob("*.sym")):
            labels.update(debugger.read_sym(sym))
        try:
            commands, layout = debugger.script(points, labels)
        except debugger.DebugError as exc:
            raise BenchError(str(exc)) from None
        trace = self.home / "emulator" / "debug.txt"
        trace.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self.busy = f"debugging {file}"
            self.last_run = file
            try:
                emu = self._open(load=str(target), extra=["--debugger-command", commands], stdout=str(trace))
                report = self._wait(emu, wait_seconds)
                screen = self._screen(emu, report)
                seen = debugger.report(trace.read_text(encoding="utf-8", errors="replace"), layout, labels)
                return f"{seen}\n\n{screen}"
            finally:
                self.busy = ""

    def _loadable(self, file: str, code: list[str] | None) -> Path:
        """The file to give the emulator: a .bas converted to a tape (with any code blocks), else the file itself."""
        source = self.path(file)
        if not source.is_file():
            raise BenchError(f"programs/{file} does not exist")
        suffix = source.suffix.lower()
        if suffix == ".bas":
            blocks = []
            for spec in code or []:
                name, sep, address = spec.rpartition("@")
                try:
                    blocks.append((self.path(name).read_bytes(), int(address, 0)))
                except (ValueError, OSError):
                    raise BenchError(f"code blocks are FILE@ADDRESS, e.g. game.bin@32768 (got {spec!r})") from None
            text = source.read_text(encoding="utf-8")
            first = next((ln.split()[0] for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")), "")
            try:
                tap = basic_to_tap(text, source.stem[:10], int(first) if first.isdigit() else None, blocks)
            except ValueError as exc:
                raise BenchError(f"the BASIC source could not be converted: {exc}") from None
            target = source.with_suffix(".tap")
            target.write_bytes(tap)
        elif suffix in LOADABLE:
            target = source
        else:
            raise BenchError(f"cannot run a {suffix or 'file without an extension'}; give a .bas, .tap, .tzx, .sna or .z80")
        return target

    def screen_text(self, wait_seconds: float = 0) -> str:
        with self._lock:
            emu = self._open()
            report = self._wait(emu, wait_seconds)
            return self._screen(emu, report)

    def screenshot(self) -> bytes:
        """The screen as a PNG, doubled in size so small details survive."""
        with self._lock:
            img = self._open().screenshot()
            self._remember(img)
            return _png(img.resize((img.width * 2, img.height * 2), Image.NEAREST))

    def press(self, keys: list[str], hold_frames: int | None = None) -> str:
        with self._lock:
            self.busy = "pressing keys"
            try:
                emu = self._open()
                for key in keys:
                    emu.press(key, hold_frames=hold_frames)
                emu.wait_frames(10)
                return self._screen(emu, emu.wait_for_report(0))
            finally:
                self.busy = ""

    def type_text(self, text: str) -> str:
        with self._lock:
            self.busy = "typing"
            try:
                emu = self._open()
                try:
                    emu.type_text(text)
                except ValueError as exc:
                    raise BenchError(str(exc)) from None
                emu.wait_frames(10)
                return self._screen(emu, emu.wait_for_report(0))
            finally:
                self.busy = ""

    def assemble(self, file: str) -> str:
        """Assemble a Z80 source file with sjasmplus into a raw .bin beside it."""
        source = self.path(file)
        if not source.is_file():
            raise BenchError(f"programs/{file} does not exist")
        out = source.with_suffix(".bin")
        out.unlink(missing_ok=True)
        cmd = [str(SJASMPLUS), "--nologo", "--msg=war", f"--raw={out}", f"--sym={source.with_suffix('.sym')}",
               f"--lst={source.with_suffix('.lst')}", f"-I{source.parent}", source.as_posix()]
        # Confined, INCBIN and INCLUDE can reach nothing outside the programs folder.
        result = subprocess.run(sandbox.command(cmd, write=[self.programs], network=False),
                                capture_output=True, text=True, cwd=source.parent, timeout=60)
        messages = (result.stdout + result.stderr).strip()
        if result.returncode or not out.is_file():
            raise BenchError(f"assembly failed:\n{messages or 'sjasmplus gave no message'}")
        rel = out.relative_to(self.programs.resolve()).as_posix()
        note = f"\n{messages}" if messages else ""
        return (f"assembled programs/{rel}: {out.stat().st_size} bytes. Labels are in the .sym file and the "
                f"listing in the .lst file beside it.{note}")

    # -- what the page can see and do -----------------------------------------

    def blank(self) -> None:
        """Switch the emulator off and forget its picture (another project is being opened)."""
        self.close()
        self._last_png = None

    def reset(self) -> None:
        """Press the Spectrum's reset button: a clean machine with nothing loaded."""
        if WINDOWS:
            raise BenchError("resetting is only available on the hosted workbench")
        self.cancel()                       # a wait for a report gives way
        with self._lock:
            if self._emu is not None:
                self._emu.close()
                self._emu = None
            self.last_run = None
            self._open()

    def keys(self, keys: list[str], down: bool) -> None:
        """A person pressing or letting go of keys while they play."""
        emu = self._emu
        if emu is None or not hasattr(emu, "key_event"):
            raise BenchError("there is no emulator to type into yet: run a program first")
        try:
            emu.key_event(keys, down)
        except (KeyError, ValueError):
            raise BenchError(f"unknown key in {keys!r}") from None

    @property
    def audio(self):
        """The running emulator's sound tap, or None."""
        return getattr(self._emu, "audio", None)

    @property
    def emulator_pid(self) -> int | None:
        return getattr(self._emu, "pid", None)

    def _remember(self, img: Image.Image) -> None:
        self._last_png = _png(img)

    def view(self) -> bytes | None:
        """The current screen for the page: never starts the emulator, never waits for a run."""
        emu = self._emu
        if emu is None and self._lock.acquire(blocking=False):
            try:
                if self._emu is None and self._window_exists():
                    self._open()
                emu = self._emu
            except SpecbotError:
                pass
            finally:
                self._lock.release()
        if emu is not None:
            # Not under the lock, so the page keeps moving while a run is being waited on.
            try:
                self._remember(emu.screenshot())
            except Exception:               # mid-restart: keep showing the last frame
                pass
        return self._last_png


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
