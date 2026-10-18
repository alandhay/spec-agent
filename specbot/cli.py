"""`specbot` command-line entry point."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Sequence

from .capture import detect_display, detect_scale
from .config import BACKENDS, CAPTURE_MODES, load_config
from .emulator import FRAME_S, emulator_from_config
from .errors import SpecbotError
from .tape import code_tap, program_tap, tokenize_program

CALIBRATE_FILE = "calibrate_raw.png"


def _keymap_arg(value: str) -> tuple[str, str]:
    key, sep, host = value.partition("=")
    if not sep or not key or not host:
        raise argparse.ArgumentTypeError("expected SPECTRUM_KEY=HOST_KEY, e.g. SYMBOL_SHIFT=ctrl")
    return key, host


def _global_options() -> argparse.ArgumentParser:
    # SUPPRESS defaults so the flags work both before and after the subcommand.
    p = argparse.ArgumentParser(add_help=False, argument_default=argparse.SUPPRESS)
    g = p.add_argument_group("global options")
    g.add_argument("--config", metavar="PATH", help="config file (default: ./specbot.toml if present)")
    g.add_argument("--backend", choices=BACKENDS, help="backend to use")
    g.add_argument("--title", metavar="TEXT", help="window title substring (window backend)")
    g.add_argument("--window-class", metavar="NAME", help="window class name (window backend)")
    g.add_argument("--capture", choices=CAPTURE_MODES, help="capture method (window backend)")
    g.add_argument("--crop", nargs=4, type=int, metavar=("L", "T", "R", "B"), help="pixels to trim off each edge")
    g.add_argument("--native-size", nargs=2, type=int, metavar=("W", "H"), help="resize captures to this size")
    g.add_argument("--hold-frames", type=int, metavar="N", help="frames to hold each key")
    g.add_argument("--gap-frames", type=int, metavar="N", help="frames between keys")
    g.add_argument("--keymap", action="append", type=_keymap_arg, metavar="KEY=HOST",
                   help="map a Spectrum key to a host key (window backend); repeatable")
    g.add_argument("--host", help="ZEsarUX host (zrcp backend)")
    g.add_argument("--port", type=int, help="ZEsarUX remote protocol port (zrcp backend)")
    g.add_argument("--launch", metavar="PATH", dest="launch_path",
                   help="emulator to start if it is not already running")
    g.add_argument("--rom", metavar="PATH", help="Spectrum ROM image, for reading screen text")
    g.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return p


def build_parser() -> argparse.ArgumentParser:
    common = _global_options()
    parser = argparse.ArgumentParser(
        prog="specbot",
        description="Screenshot and keypress automation for ZX Spectrum emulators.",
        parents=[common],
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p = sub.add_parser("screenshot", parents=[common], help="save the emulator display as a PNG")
    p.add_argument("path", help="output image file")

    p = sub.add_parser(
        "press", parents=[common], help="press keys one after another; join a chord with '+'",
        description="Press keys in sequence, e.g. `specbot press J ENTER`. "
        "Join keys pressed together with '+', e.g. `specbot press SYMBOL_SHIFT+P`.",
    )
    p.add_argument("keys", nargs="+", metavar="KEY", help="key, alias (DELETE, BREAK ...) or chord")

    p = sub.add_parser(
        "type", parents=[common], help="type a literal string (\\n = ENTER)",
        description=r"Type text letter by letter. \n means ENTER. Use '-' to read the text from stdin.",
    )
    p.add_argument("text", help=r"text to type; \n is ENTER, '-' reads stdin")

    p = sub.add_parser("wait", parents=[common], help="wait for a number of frames (20 ms each)")
    p.add_argument("frames", type=int)

    def wait_report_option(p):
        p.add_argument("--wait-report", type=float, metavar="SECONDS", nargs="?", const=30.0,
                       help="wait (default up to 30 s) for a BASIC report such as '0 OK, 20:1', "
                       "then print the screen text; exits with status 2 if none appears")

    p = sub.add_parser(
        "text", parents=[common],
        help="print the text on screen (needs native_size and a ROM image)",
        description="Print the 24 rows of screen text (characters only: colours and "
        "inverse video are not shown; graphics print as '?'). Trailing spaces are stripped.",
    )
    wait_report_option(p)

    def tape_options(p):
        p.add_argument("--autostart", type=int, metavar="LINE",
                       help="line to run from after loading (default: the first line)")
        p.add_argument("--no-autostart", action="store_true", help="load without running")
        p.add_argument("--name", help="tape file name, max 10 characters (default: source file name)")
        p.add_argument("--code", action="append", default=[], metavar="FILE@ADDR",
                       help='append a CODE block for LOAD "" CODE; repeatable')

    p = sub.add_parser(
        "bas2tap", parents=[common], help="convert BASIC source text to a .tap file",
        description="Convert BASIC source (UPPER CASE keywords, numbered lines) to a tape file. "
        "No emulator is needed.",
    )
    p.add_argument("source", help="BASIC source file")
    p.add_argument("output", nargs="?", help="output .tap (default: source with .tap extension)")
    tape_options(p)

    p = sub.add_parser(
        "load", parents=[common], help="load a .bas, .tap or snapshot file into the emulator",
        description="Load a file into the emulator. A .bas file is converted to .tap first. "
        "The window backend restarts the emulator with the file (needs [launch]).",
    )
    p.add_argument("file", help=".bas source, or a tape/snapshot file the emulator understands")
    tape_options(p)
    wait_report_option(p)

    sub.add_parser(
        "calibrate", parents=[common],
        help="detect the Spectrum display and suggest crop / native_size",
    )
    return parser


def _overrides(args: argparse.Namespace) -> dict:
    names = (
        "backend", "title", "window_class", "capture", "crop", "native_size",
        "hold_frames", "gap_frames", "host", "port", "launch_path", "rom",
    )
    overrides = {name: getattr(args, name) for name in names if hasattr(args, name)}
    if hasattr(args, "keymap"):
        overrides["keymap"] = dict(args.keymap)
    return overrides


def _unescape(text: str) -> str:
    out, i = [], 0
    while i < len(text):
        pair = text[i : i + 2]
        if pair == "\\n":
            out.append("\n")
            i += 2
        elif pair == "\\\\":
            out.append("\\")
            i += 2
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _calibrate(emu, config) -> None:
    raw = emu.raw_screenshot(CALIBRATE_FILE)
    print(f"Saved raw screenshot to {CALIBRATE_FILE} ({raw.width}x{raw.height})")
    box = detect_display(raw)
    if box is None:
        raise SpecbotError(
            "could not find a Spectrum display with a uniform border in the screenshot; "
            f"inspect {CALIBRATE_FILE} and set crop by hand"
        )
    left, top, right, bottom = box
    width, height = right - left, bottom - top
    crop = [left, top, raw.width - right, raw.height - bottom]
    scale = detect_scale(raw.crop(box))
    native = [width // scale, height // scale]
    print(f"Detected display: {width}x{height} at ({left}, {top})")
    if scale > 1:
        print(f"Detected scale factor: {scale}x (native {native[0]}x{native[1]})")
    else:
        print("Detected scale factor: 1x (if the window is enlarged, the emulator is filtering or scaling unevenly)")
    print()
    print("Suggested config:")
    print()
    print(f"[{config.backend}]")
    print(f"crop = {crop}")
    print(f"native_size = {native}")


def _parse_code_block(spec: str) -> tuple[bytes, int]:
    file, sep, address = spec.rpartition("@")
    try:
        if not sep:
            raise ValueError
        return Path(file).read_bytes(), int(address, 0)
    except ValueError:
        raise SpecbotError(f"--code expects FILE@ADDRESS, e.g. game.bin@32768 (got {spec!r})") from None


def _bas2tap(args: argparse.Namespace, source: Path, output: Path) -> Path:
    program = tokenize_program(source.read_text(encoding="utf-8"))
    if args.no_autostart:
        autostart = None
    elif args.autostart is not None:
        autostart = args.autostart
    else:
        autostart = int.from_bytes(program[:2], "big")  # the first line
    tap = program_tap(program, args.name or source.stem, autostart)
    for index, spec in enumerate(args.code, 1):
        data, address = _parse_code_block(spec)
        tap += code_tap(data, address, f"code{index}")
    output.write_bytes(tap)
    return output


def _run(args: argparse.Namespace) -> None:
    config = load_config(getattr(args, "config", None), **_overrides(args))
    if args.command == "wait":
        # Both backends approximate frames as 20 ms, so no emulator is needed.
        if args.frames < 0:
            raise SpecbotError("frames must not be negative")
        time.sleep(args.frames * FRAME_S)
        return
    if args.command == "bas2tap":
        source = Path(args.source)
        output = _bas2tap(args, source, Path(args.output) if args.output else source.with_suffix(".tap"))
        print(f"Wrote {output} ({output.stat().st_size} bytes)")
        return
    load = None
    if args.command == "load":
        load = Path(args.file)
        if load.suffix.lower() == ".bas":
            load = _bas2tap(args, load, load.with_suffix(".tap"))
    with emulator_from_config(config, load=str(load) if load else None) as emu:
        if args.command == "screenshot":
            img = emu.screenshot(args.path)
            print(f"Saved {args.path} ({img.width}x{img.height})")
        elif args.command == "press":
            emu.tap_sequence(args.keys)
        elif args.command == "type":
            text = sys.stdin.read() if args.text == "-" else _unescape(args.text)
            emu.type_text(text)
        if args.command == "load":
            print(f"Loaded {load}")
        if args.command == "text" or getattr(args, "wait_report", None) is not None:
            report = True
            if args.wait_report is not None:
                report = emu.wait_for_report(args.wait_report)
            print("\n".join(emu.screen_text()))
            if not report:
                print(f"specbot: no BASIC report after {args.wait_report:g} s "
                      "(still running, waiting for input, or crashed)", file=sys.stderr)
                raise SystemExit(2)
        if args.command == "calibrate":
            _calibrate(emu, config)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # screen text may hold a pound sign
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )
    try:
        _run(args)
    except KeyboardInterrupt:
        print("specbot: interrupted", file=sys.stderr)
        return 130
    except (SpecbotError, ValueError, OSError) as exc:
        print(f"specbot: error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
