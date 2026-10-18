# spec-agent

An AI agent that writes ZX Spectrum programs and checks them on a real emulator.

You describe a program ("a game where a frog crosses the road", "draw the
Mandelbrot set"). The agent writes Sinclair BASIC, or Z80 assembly where speed
matters, loads it into Fuse, reads the result back off the screen and fixes it
until it works. Every claim it makes about a program is based on what the
emulator actually showed.

This repository holds the toolkit that makes that possible and the projects
built on it:

| Folder | What it is |
|---|---|
| [`specbot/`](specbot/README.md) | The foundation: a Python package and CLI that drives a Spectrum emulator from scripts. It takes screenshots, sends keys, turns BASIC text into `.tap` tapes and reads the screen as exact text (by matching cells against the ROM font, not by OCR). |
| [`specdev/`](specdev/README.md) | The coding agent, with a web page. You chat with it, watch every step it takes, and see the source and the emulator's live screen. Built on the Claude Agent SDK. |
| [`deploy/`](deploy/README.md) | Hosting `specdev` on AWS for invited users: CloudFormation, Cognito sign-in, a headless Fuse per user, per-user budgets, an instance that switches itself off when unused, and an MCP server so Claude, ChatGPT and other assistants can use the workbench as a tool. |
| [`specart/`](specart/README.md) | Image → Spectrum picture converter (CLI and web page). It runs the result on the emulator and scores how close the screen is to the original, including 128K "multicolour" mode with 8x2 colour cells. |
| [`tetris/`](tetris/README.md) | Tetris in Z80 machine code, with a scripted test suite that plays itself on the emulator and checks the well, score and frame timing. |
| [`jupiter/`](jupiter/README.md) | The one-off Hubble-Jupiter conversion that `specart` grew out of. |
| `programs/` | BASIC programs written by the agent (`.bas` source plus generated `.tap`). |
| `.claude/agents/` | Claude Code subagents: `spectrum-dev` (write/run/debug programs through the `specbot` CLI) and `spectrum-artist` (convert images through `specart`). |

## How it fits together

```
           you ──► specdev web page / MCP client / Claude Code subagent
                              │
                     agent (Claude) ── tools: write file, run, read screen,
                              │               screenshot, press keys, assemble
                              ▼
                    specbot ── tape.py: BASIC → .tap
                              ── ocr.py: screen → text
                              ── backend: Win32 window │ ZEsarUX TCP │ Xvfb on Linux
                              ▼
                         Fuse (128K Spectrum)
```

The main loop is: write → convert to tape → restart the emulator with that
tape → wait for the BASIC report on the bottom line (`0 OK, 170:1`) → read
the screen. This needs no keyboard focus, so the emulator can stay in the
background while you work.

## Getting started (Windows)

You need Windows 10/11, Python 3.11+, and [Fuse](https://fuse-emulator.sourceforge.net/)
installed at `C:/Program Files (x86)/Fuse/` (or edit `specbot.toml`).

```
python -m venv .venv
.venv\Scripts\pip install -e ".[dev,agent]"
.venv\Scripts\specbot load programs\hello.bas --wait-report 60   # run a program and print the screen it leaves
```

Then:

```
.venv\Scripts\python -m specdev            # the coding agent: http://127.0.0.1:8766 (uses your Claude Code login)
.venv\Scripts\python -m specart serve      # the picture converter: http://127.0.0.1:8765 (needs numpy and numba)
.venv\Scripts\python tetris\tests.py       # build Tetris and run its tests on the emulator
.venv\Scripts\python -m pytest             # unit tests (no emulator needed)
```

In Claude Code you can also just ask: "use spectrum-dev to write a Spectrum
program that …".

`CLAUDE.md` describes the architecture in detail. Each folder's README covers
its own options.

## Status

- **specbot, specart, tetris**: working, with tests.
- **specdev**: works locally and hosted. The page gets updates pushed from
  the server as they happen. The agent keeps short notes about each person
  across conversations and projects (shown under **Memory**), keeps earlier
  versions of each project's programs (**History**), and can make a public
  link that plays a program in any browser (**Share**). Hosted, an admin page
  invites people, adds credit and suspends people. Hosted, each
  person's agent, assembler and emulator are confined to their own folders
  with Linux Landlock. They still share one machine, so invite only people
  you trust.
- **MCP server**: tools for listing, opening and creating projects and for
  building and checking programs. Hosts that support MCP Apps also show the
  live Spectrum (screen, sound, tape download) in the chat; checked against
  the spec in a test host, not yet inside Claude or ChatGPT.
- CI (GitHub Actions) runs the unit tests on every push and, on a Linux
  runner, the Landlock self-check, Claude Code starting while confined, and
  the workbench smoke test (Fuse and sjasmplus, confined).

## Notes

- Typing is literal, as in the 128K editor. 48K keyword entry is not
  supported.
- Multicolour pictures are timed for the 128K (228 T-states per line) and do
  not display correctly on a 48K machine.
- `tetris/tools/` bundles the [sjasmplus](https://github.com/z00m128/sjasmplus)
  assembler (BSD licence).
- The sample images in `specart/samples/` and `jupiter/source/` are public
  domain.
