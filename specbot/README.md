# specbot

Screenshot and keypress automation for a ZX Spectrum emulator running in a
window on Windows. A script (or an AI agent) can:

- **See** – capture the emulator display as a PNG / PIL image.
- **Act** – send Spectrum keys, chords and whole strings of text.
- **Wait** – pause for a number of frames, or until the screen changes.

Two interchangeable backends sit behind one interface:

| Backend | Works with | Capture | Keys | Needs focus? |
|---|---|---|---|---|
| `window` (default) | Any emulator (Fuse, ZEsarUX, Spectaculator, SpecEmu) | Screen grab of the window's client area | `SendInput` scan codes | Yes, for keys |
| `zrcp` | ZEsarUX only | Remote protocol `save-screen` | Remote protocol `send-keys-event` | No |

## Install

Windows 10/11, Python 3.11+. No admin rights, drivers or hooks.

```
pip install -e .          # add ".[dev]" for pytest
specbot --help
```

## Quick start

```
copy specbot.toml.example specbot.toml     # then edit window_title etc.
specbot screenshot shot.png
specbot press J ENTER                      # keys one after another
specbot press SYMBOL_SHIFT+P               # '+' joins a chord
specbot type 'PRINT "HELLO"\n'             # \n is ENTER
specbot wait 50                            # frames (20 ms each)
specbot calibrate                          # suggest crop / native_size
```

```python
import specbot

with specbot.open_emulator(window_title="Fuse", native_size=(320, 240)) as emu:
    before = emu.screenshot()
    emu.type_text('PRINT "HELLO"\n')
    emu.wait_for_change(baseline=before)
    emu.screenshot("hello.png")
    emu.press("BREAK")                     # alias for CAPS_SHIFT + SPACE
    emu.tap_sequence(["J", ("SYMBOL_SHIFT", "P"), "SYMBOL_SHIFT+P", "ENTER"])
```

`open_emulator(config_path=None, **overrides)` reads `specbot.toml` from the
current directory (or `$SPECBOT_CONFIG`, or the path you pass) and applies the
keyword overrides on top. With no file at all, defaults plus overrides are used.

> **Shell quoting.** In PowerShell 7.3+ and bash, `specbot type 'PRINT "HELLO"\n'`
> works as written. Windows PowerShell 5.1 strips embedded double quotes from
> arguments; there, pipe the text instead: `'PRINT "HELLO"' | specbot type -`
> (stdin is typed literally, including its trailing newline).

## Developing Spectrum programs

Three more commands make an edit–run–observe loop that needs no keyboard
input, so the emulator can stay in the background:

```
specbot load programs/hello.bas --wait-report 60   # convert, run, wait for the BASIC report, print the screen
specbot load programs/hello.bas     # just convert to .tap and restart the emulator with it
specbot text                        # the 24 rows of screen text, incl. the BASIC report line
specbot text --wait-report 30       # wait for a report first; exit status 2 if none appears
specbot bas2tap prog.bas [out.tap]  # just convert (no emulator needed)
```

- **`load FILE`** takes a `.bas` source file (converted first) or anything the
  emulator opens from its command line (`.tap`, `.tzx`, `.sna`, `.z80`). With
  the window backend it *restarts* the emulator with the file, so it needs a
  `[launch]` section; it only ever closes a process whose executable matches
  `launch.path`. With `zrcp` it uses `smartload`. Options: `--autostart LINE`
  (default: first line), `--no-autostart`, `--name`, and `--code FILE@ADDR` to
  append machine-code blocks for `LOAD "" CODE`.
- **`text`** reads the screen by matching character cells against the ROM font,
  so it is exact rather than OCR guesswork; graphics/UDG cells print as `?`.
  It needs `native_size` set, and a ROM image: `rom = "path/to/48.rom"` at the
  top of the config, or automatically `roms/48.rom` beside `launch.path`
  (which is where Fuse keeps it).
- **BASIC source format**: one numbered line per text line; keywords in UPPER
  CASE (lower-case words are never tokenised, so use lower-case variables);
  `#` lines are comments; zmakebas-style escapes (`\a`–`\u` UDGs, `\{N}` any
  code, `\::` block graphics). See `specbot/tape.py`.

From Python: `emu.load_file(path)`, `emu.screen_text()`, and
`specbot.tape.basic_to_tap(source, autostart=10)`.

### The `spectrum-dev` agent

`.claude/agents/spectrum-dev.md` defines a Claude Code subagent that uses this
loop to write, run, test and debug Spectrum BASIC programs in `programs/`. In
Claude Code, ask for it by name ("use spectrum-dev to write a Spectrum
program that ...") or just describe a Spectrum programming task. It expects
the `specbot.toml` in this folder (Fuse, 128K mode).

## Key names

The 40 Spectrum keys, case-insensitive: `0`–`9`, `A`–`Z`, `ENTER`, `SPACE`,
`CAPS_SHIFT`, `SYMBOL_SHIFT`.

Aliases that resolve to chords:

| Alias | Chord |
|---|---|
| `DELETE` / `BACKSPACE` | `CAPS_SHIFT` + `0` |
| `LEFT` / `DOWN` / `UP` / `RIGHT` | `CAPS_SHIFT` + `5` / `6` / `7` / `8` |
| `EDIT` | `CAPS_SHIFT` + `1` |
| `BREAK` | `CAPS_SHIFT` + `SPACE` |
| `EXTEND` | `CAPS_SHIFT` + `SYMBOL_SHIFT` |

`type_text` / `specbot type` types characters literally: lowercase letters are
the plain key, uppercase use `CAPS_SHIFT`, and these symbols use `SYMBOL_SHIFT`:

```
! @ # $ % & ' ( ) _      keys 1 2 3 4 5 6 7 8 9 0
< > ; " ^ - + = : £ ? / * , .      keys R T O P H J K L Z X C V B N M
```

Anything else raises `ValueError` naming the character and its position before
any key is sent. This is right for the 128K BASIC editor, games and most
prompts. It does **not** do 48K keyword entry (where `J` means `LOAD`); send
keyword keys yourself with `press`.

## `window` backend

Set `window_title` (case-insensitive substring) and/or `window_class` (exact).
Exactly one visible top-level window must match; otherwise the error lists the
titles that did.

**Capture** grabs the window's *client area* (no title bar or frame), and the
process is per-monitor DPI aware so it is correct on scaled displays. Emulator
chrome inside the client area (status bar, toolbar) is removed with `crop`, and
`native_size` scales the result down so one Spectrum pixel is one image pixel.
Run `specbot calibrate` to get both values: it saves `calibrate_raw.png`, finds
the Spectrum display and prints a TOML snippet.

- `capture = "mss"` (default) reads the screen, so the window must be visible
  and uncovered.
- `capture = "printwindow"` asks Windows to render the window, so it works when
  the window is covered. Some emulators/renderers return a black image; specbot
  logs a warning suggesting `mss` when that happens.
- A minimised window raises an error rather than returning a blank image.

**Keys** are sent as scan codes with `SendInput`, so the emulator must be the
foreground window. specbot brings it forward before every keypress (and raises
`FocusError` if Windows refuses), so keys never land in another application.
Don't use the keyboard yourself while a script is typing.

Timing: modifiers go down, one frame later the main key, held for
`hold_frames` (default 3 = 60 ms), then released main-key-first, followed by a
`gap_frames` pause (default 3). Raise both if characters are dropped, e.g. in a
slow BASIC editor or with the emulator running faster/slower than 100 %. All
keys are released in a `finally` block and again at exit, so Ctrl+C never
leaves a key stuck down.

`wait_frames(n)` sleeps `n × 20 ms`; the backend cannot see emulated frames, so
this is an approximation that assumes the emulator runs at normal speed.

### Host keymap per emulator

`[window.keymap]` maps Spectrum keys to host key names
([pydirectinput names](https://github.com/learncodebygaming/pydirectinput):
`shift`, `shiftright`, `ctrl`, `ctrlright`, `alt`, `altright`, `enter`,
`space`, letters, digits ...). Only list the keys that differ from the default
(`CAPS_SHIFT = "shift"`, `SYMBOL_SHIFT = "ctrl"`, everything else the same key).

| Emulator | `CAPS_SHIFT` | `SYMBOL_SHIFT` | Notes |
|---|---|---|---|
| Fuse | `shift` | `ctrl` | Defaults work. Tested with Fuse 1.6.0. |
| ZEsarUX | `shift` | `ctrl` | Defaults work; or use the `zrcp` backend. |
| Spectaculator | `shift` | `ctrl` | Uses Ctrl for Symbol Shift by default; check Tools → Options → Keyboard and use the "Spectrum" (positional) layout. Not tested here. |
| SpecEmu | `shift` | `ctrl` | Check its keyboard options if symbols come out wrong. Not tested here. |

If symbols come out wrong, the emulator is usually translating PC punctuation
itself or maps Symbol Shift to another key: switch it to a positional/"Spectrum"
keyboard layout, or change `SYMBOL_SHIFT` (e.g. `"ctrlright"` or `"altright"`).
On the command line: `--keymap SYMBOL_SHIFT=ctrlright`.

## `zrcp` backend (ZEsarUX)

Start ZEsarUX with the remote protocol enabled, then select the backend:

```
zesarux --enable-remoteprotocol            # --remoteprotocol-port N to change from 10000
specbot --backend zrcp screenshot shot.png
specbot --backend zrcp type 'PRINT "HELLO"\n'
```

Keys and screenshots go over TCP, so the ZEsarUX window can be in the
background or covered. Notes:

- Screenshots are saved by ZEsarUX to a temporary BMP and read back, so
  ZEsarUX must run on the same machine as specbot.
- Keys are sent with `send-keys-event` using ZEsarUX's internal key numbers.
  The defaults match current ZEsarUX source; if a shift key does nothing on an
  older version, override the numbers in `[zrcp.keycodes]` (see the notes at
  the top of `specbot/zrcp_backend.py`).
- The protocol has no "wait N frames" command, so `wait_frames` sleeps
  `n × 20 ms` as in the window backend.
- With a `[launch]` section, specbot starts ZEsarUX with
  `--enable-remoteprotocol` added automatically if it cannot connect.

## Configuration

See [`specbot.toml.example`](../specbot.toml.example). Every field can also be
given as a keyword to `open_emulator()` or as a CLI flag:

| TOML | `open_emulator()` keyword | CLI flag |
|---|---|---|
| `backend` | `backend` | `--backend` |
| `window.window_title` | `window_title` (or `title`) | `--title` |
| `window.window_class` | `window_class` | `--window-class` |
| `window.capture` | `capture` | `--capture` |
| `crop`, `native_size` | `crop`, `native_size` | `--crop L T R B`, `--native-size W H` |
| `hold_frames`, `gap_frames` | `hold_frames`, `gap_frames` | `--hold-frames`, `--gap-frames` |
| `window.keymap` | `keymap={...}` | `--keymap KEY=HOST` (repeatable) |
| `zrcp.host`, `zrcp.port` | `host`, `port` | `--host`, `--port` |
| `zrcp.keycodes` | `keycodes={...}` | – |
| `launch.path`, `launch.args` | `launch_path`, `launch_args` | `--launch PATH` |

`crop`, `native_size`, `hold_frames` and `gap_frames` exist in both `[window]`
and `[zrcp]`; overrides apply to the selected backend. Other global CLI flags:
`--config PATH` and `-v` (debug logging). Flags may go before or after the
command. Failures print one `specbot: error: ...` line and exit non-zero.

## Tests

```
pytest                    # unit tests: no emulator needed, any OS
pytest -m integration     # needs Windows + an emulator sitting in 128K BASIC
```

The integration tests use `specbot.toml` in the current directory (or
`$SPECBOT_CONFIG`).

## Not included

48K keyword-mode text entry, joystick emulation, an MCP server wrapper, and
memory/register inspection over ZRCP.
