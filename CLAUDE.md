# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Windows-only toolkit for driving a ZX Spectrum emulator (Fuse, 128K mode) from scripts, plus projects built on it:

- `specbot/` – the installable package: screenshot, keypress, tape-building and screen-reading automation. Everything else depends on it.
- `specdev/` – the coding agent with a web front end: describe a program, watch the agent write, run and debug it on the emulator.
- `specart/` – image → Spectrum picture converter (CLI and local web page) that runs the result on the emulator and scores it.
- `tetris/` – a Z80 machine-code game with a scripted on-emulator test suite.
- `jupiter/` – the one-off conversion that `specart` was generalised from; it has its own older `convert.py`, not shared with `specart/convert.py`.
- `deploy/` – hosting `specdev` on AWS (stack `specdev`, us-east-1): CloudFormation template, instance install script, `deploy.py`, and `check.sh`/`smoke.py` to test it (see `deploy/README.md`). The hosted page is served from S3, not by `server.py`, and the instance stops itself when unused.
- `programs/` – BASIC programs written with the `spectrum-dev` agent (`.bas` plus generated `.tap`; scratch output in `programs/out/`).

Git repository: https://github.com/alandhay/spec-agent. `backups/` (not checked in) holds older zip snapshots.

## Commands

The project path contains a space, so quote it. Use the venv directly (`.venv/Scripts/python.exe`, `.venv/Scripts/specbot.exe`); `specbot` is installed editable from this folder.

```
pip install -e ".[dev]"                               # specbot + pytest
.venv/Scripts/python.exe -m pytest                    # unit tests; integration tests are deselected by default
.venv/Scripts/python.exe -m pytest tests/test_keys.py::test_name
.venv/Scripts/python.exe -m pytest --ignore=tests/test_specart.py   # skip the slow numba-compiled specart tests
.venv/Scripts/python.exe -m pytest -m integration     # needs Fuse sitting in the 128K BASIC editor
```

`specart` and `specdev` are not listed packages in `pyproject.toml`; run them from the project root with `python -m specart` / `python -m specdev`. `specart` needs `numpy` and `numba` (undeclared, installed in `.venv`); `specdev` needs `claude-agent-sdk` (`pip install -e ".[agent]"`). pytest takes ~25 s just to start in this OneDrive folder. `tests/test_specart.py` skips itself when numba is missing. The numba kernels compile on first use (cached afterwards), which makes the first specart test run or conversion slow.

Emulator loop (no keyboard focus needed; each `load` restarts Fuse with a clean machine):

```
.venv/Scripts/specbot.exe load programs/hello.bas --wait-report 60   # convert, run, wait for the BASIC report, print the screen; exit 2 = no report
.venv/Scripts/specbot.exe text                                       # 24 rows of screen text
.venv/Scripts/specbot.exe screenshot programs/out/x.png              # 320x240 including border
.venv/Scripts/specbot.exe bas2tap prog.bas [out.tap]                 # convert only, no emulator
```

Sub-projects (run from their own folder with `..\.venv\Scripts\python.exe`, or by path from the root):

```
python -m specart convert photo.jpg [--mode multicolour|standard] [--fit fit|fill|stretch] [--tone original|lift|lift-more] [--effort quick|thorough] [--out DIR]
python -m specart serve                # http://127.0.0.1:8765
python -m specdev [--port 8766]        # coding agent page, http://127.0.0.1:8766
python deploy/deploy.py                # push the current code to the hosted instance and restart it
python tetris/build.py                 # -> tetris/build/tetris.tap and .sna
python tetris/build.py --test NAME     # scripted self-playing build
python tetris/tests.py [NAME ...] [-v] # build + run on the emulator; --look decodes the current screen
python tetris/keytest.py               # real key presses: takes the focus
python jupiter/build.py [--fresh] [--verify]
```

## Architecture

### specbot

`Emulator` (`emulator.py`) is an abstract base: backends implement only `_capture_raw()` and `_press_chord()` (and `load_file()`); `screenshot`, `screen_text`, `wait_for_report`, `press`, `type_text`, `wait_for_change` are built on those. `open_emulator()` → `load_config()` → `emulator_from_config()` picks the backend.

- `window_backend.py` – any emulator, via Win32: finds exactly one matching window, captures its client area (`mss` screen grab or `PrintWindow`), sends scan codes with `SendInput` through `ChordPlayer`. `load_file` works by *killing and relaunching* the emulator with the file on its command line, so it needs `[launch]` and only terminates a process whose exe matches `launch.path`.
- `zrcp_backend.py` – ZEsarUX only, over its TCP remote protocol.
- `x11_backend.py` – Linux servers: each `X11Emulator` owns an Xvfb display with one Fuse on it (capture and XTEST keys through python-xlib). Not selectable from `specbot.toml`; `specdev` builds it directly when not on Windows.
- `config.py` – `specbot.toml` (cwd, or `$SPECBOT_CONFIG`, or explicit path) into dataclasses, with keyword/CLI overrides applied on top. `crop`, `native_size`, `hold_frames`, `gap_frames` exist per backend; overrides go to the active one.
- `keys.py` – the 40 Spectrum keys, aliases (`BREAK`, `DELETE`, arrows…) resolved to chords, and literal text → chords. Typing is literal (128K editor style); 48K keyword entry is not supported.
- `tape.py` – BASIC source text → tokenised program → `.tap` blocks (`tokenize_program`, `program_tap`, `code_tap`, `basic_to_tap`). The other sub-projects import this to write their tapes.
- `ocr.py` – reads screen text by exact match of 8x8 cells against the ROM font (needs `native_size` and a 48K ROM; found at `roms/48.rom` beside `launch.path`). Non-text cells come back as `?`.
- `capture.py` – crop/scale post-processing and the display detection behind `specbot calibrate`.

Key input pulls the emulator window to the foreground and anything the user types lands in it; capture with `capture = "printwindow"` (the setting in this folder's `specbot.toml`) and tape loading do not. Prefer designs that are testable without sending keys.

Unit tests run without Windows APIs or an emulator: the backends take injected drivers/sleep functions, which is how `tests/test_chords.py`, `test_emulator.py` and `test_zrcp.py` exercise timing and protocol logic.

### specdev

Three layers, each usable without the one above:

- `bench.py` – `Bench`: the blocking operations on the one emulator (run a program, read the screen, screenshot, keys, assemble with sjasmplus), under one lock, returning text worded for the agent. All file names are confined to the bench's `programs` folder. On Windows it drives the one Fuse window; elsewhere it starts a private headless Fuse (`x11_backend`). `view()` serves the page's live screen without the lock and never starts the emulator.
- `agent.py` – `Session`: one Claude Agent SDK conversation on its own thread and event loop. The agent has no shell: built-in `Read`/`Glob`/`Grep`/`Write`/`Edit`, all confined to the bench's folder by `make_guard` (a permission callback plus a pre-tool hook: the callback alone is not asked about every read), and the bench exposed as in-process MCP tools (`make_tools`). It uses the installed Claude Code login (no API key); `SPECDEV_MODEL` overrides the model. Everything the agent says and does is appended to `Session.events`; reset bumps `generation`; every change bumps `version`, which `wait_change()` waits on. With a `Memory` (`memory.py`, the person's `memory.json`) it also gets `remember`/`forget` tools and its notes are appended to the system prompt.
- `sandbox.py` – with `SPECDEV_SANDBOX=1` (hosted) every process started for a person (Fuse, sjasmplus, and Claude Code through a generated `agent.sh` launcher) is run through it: Landlock confines it to reading system paths and its own folders, drops `SPECDEV_*` from its environment, and refuses to run the command if the kernel cannot confine. Standard library only, because it is run as a script by path. `python specdev/sandbox.py --check` tests it on a machine; only Linux can.
- `projects.py` – `Project` (a folder: programs, `chat.jsonl`, `project.json`, thumbnail, and `history/` versions of the sources with `keep_version`/`restore`) and `Library` (one person's projects, copied to S3 when a bucket is set and fetched back when the local copy is missing). `PROJECTS.md` is the spec for projects, the library, hand-written programs and image uploads.
- `debugger.py` – breakpoints for the agent's `debug_program` tool: turns breakpoints given with registers, labels (from the assembler's `.sym`) and `byte`/`word X` into a Fuse `--debugger-command` script (each hit prints a marker, PC and the values, then continues; `$hN` counters limit hits) and parses Fuse's stdout back into a line per hit. Linux backend only: `Bench.debug` restarts the headless Fuse with the script and its stdout in a file. Fuse 1.6.0 crashes at start-up if SDL's "dummy" audio driver is used (as on CI without sound) - the workbench's disk audio avoids it. Register names in Fuse expressions are `z80:hl`, `z80:af_` and so on; bare `PC` is a syntax error.
- `share.py` – share links: `Shares` writes `play/<id>.html|.tap|.bas` under `DATA/shared` (served by `server.py` locally) and, hosted, to `site/play/` in the bucket; the page runs the tape in JSSpeccy 3, fetched from its pinned release (`player_files`) into `DATA/player` locally and into `site/player/` by `deploy.py`.
- `admin.py` + `static/admin.html` – the admin page: everyone with a folder or a Cognito account, with their `person.json` (address, last seen, suspended), budget and request log; invite, grant credit, suspend (Cognito plus a server-side refusal, since ID tokens last 24 h). Admins are `SPECDEV_ADMINS`; locally, you.
- `mcp.py` – an MCP server at `/mcp/<key>` so assistants can drive one person's workbench (see `deploy/README.md`).
- `server.py` + `static/index.html` – stdlib HTTP server on 127.0.0.1. The page follows the SpecDev Workbench design (direction 2a): a project rail, a top bar with the status pill, the agent's steps as coloured rows, and a strip of the agent's runs under the screen (each run's last frame is kept in the project's `runs/` folder by `Workspace._run_frame`); what the page shows of the agent (events, status, the reply being written, budget, files) is pushed on `/api/events.stream` (`event_frames`) as it changes, and the emulator's screen and sound on `/api/screen.stream` and `/api/sound.stream`, all long length-prefixed responses (`/api/events` remains for scripts such as `deploy/check.sh`) and key presses are posted to `/api/keys`. Sound, live keys, reset and server stats need the Linux backend. POSTs must be JSON (keeps other sites from posting to it). Locally there is one `Workspace` (person `local`, projects under `data/local/`, no sign-in; the top-level `programs/` folder is not used by it). With `SPECDEV_COGNITO_*` set (the hosted case) each request needs a Cognito ID token in a cookie and each person gets their own `Workspace` (folder under `SPECDEV_DATA`, bench, session), with a budget (`account.json`: a starting amount plus top-ups, less the cost of their requests; `top_up` is where a real service would take payment), a log of every request and its cost (`log.jsonl`), and idle sleep.

`prompt.md` is the agent's system prompt. It overlaps with `.claude/agents/spectrum-dev.md` (which describes the same loop through the `specbot` CLI); a change to the BASIC rules or working method belongs in both.

### specart

`pipeline.Job` runs one conversion end to end and reports progress (polled by `server.py`/`static/index.html`, followed by the CLI): open → fit to 256x192 in linear light → error-diffusion dither → direct-search refinement against an eye model (`convert.py`, inner loops in `_kernels.py` under numba) → encode → build tape → run in Fuse through specbot → score the captured screen and compare it pixel for pixel with the intended picture. `EMULATOR_LOCK` allows one job at a time because there is one emulator. Jobs are kept in `specart/jobs/<id>/`.

`multicolour.py` generates ~9 KB of straight-line Z80 that rewrites attributes in step with the TV beam (8x2 colour cells over the middle 24 columns). It simulates T-states including memory contention to place every write, and the timings are for the 128K (228 T-states per line) – it does not display correctly on a 48K machine. The pixel-exact emulator comparison is the test that this timing is right.

### tetris

`build.py` generates `src/pieces.inc` (and `src/test.inc` for test builds), assembles `src/tetris.asm` with the bundled `tools/sjasmplus-1.24.0.win/sjasmplus.exe`, and wraps the code in a tape with a BASIC loader using `specbot.tape`. Both `.inc` files are generated – edit `PIECES` in `build.py` or `TESTS` in `tests.py`, not the includes.

Tests avoid key input entirely: with `-DTEST=1` the game skips the title screen, takes pieces from a fixed list and "keys" from a frame-counted script (tokens like `L1 .1 D1 .40`), then returns to BASIC, which prints frame counters and `STOP`s. `tests.py` waits for the `9 STOP` report and reads score, lines, level and the well contents off the emulator screen, and checks no frame was dropped. The game draws only by changing colour attributes right after the frame interrupt, which is why the tests decode the well from colours.

## Conventions

- BASIC source for `bas2tap`: one numbered line per text line, keywords UPPER CASE, variables lower case (lower-case words are never tokenised), `#` lines are comments, zmakebas-style escapes. Format errors are caught before the emulator is touched; BASIC syntax errors only appear at run time as a report.
- A BASIC report on the last screen row (`0 OK, 170:1`) is the signal that a program finished; a program that loops forever or waits at INPUT/`scroll?` never produces one.
- Don't claim a Spectrum program or picture works without running it on the emulator and reading the result back.
- `.claude/agents/spectrum-dev.md` and `spectrum-artist.md` hold the detailed working procedures for writing Spectrum programs and converting images; delegate to those agents or follow those files. Note `spectrum-dev.md` says no assembler is installed – sjasmplus is available under `tetris/tools/` (the `specdev` agent uses it).
