# SpecDev

Describe a ZX Spectrum program; an agent writes it, runs it on the emulator,
reads the result off the screen and fixes it until it works. A web page shows
the agent's messages and every step it takes, the program source, and the
emulator's screen live.

## Use it

    ..\.venv\Scripts\python.exe -m specdev        # then open http://127.0.0.1:8766

Needs Claude Code installed and logged in (the agent runs through it; no API
key), Fuse set up as in `specbot.toml`, and `pip install claude-agent-sdk`.

- **Send** a request; while the agent is working, further messages queue.
- **Stop** interrupts the agent. **New conversation** forgets the chat; the
  files in `programs/` stay.
- Click a step to see what the agent asked for and what came back (screen
  text, the BASIC report, screenshots).
- The Program pane follows the file the agent is working on; click a tab to
  pin another. Tapes can be downloaded from there.

## What the agent can do

Read files; write only inside `programs/`; run a `.bas` (or tape/snapshot) on
a freshly restarted emulator and wait for the BASIC report; read the screen
text; take a screenshot; press keys or type; assemble Z80 with sjasmplus and
load the code from a BASIC loader; hosted, also run a program with breakpoints in
its machine code and see registers and memory at each one. It has no shell.

It keeps a few notes about you from one conversation to the next, in all your
projects (`remember` and `forget` tools; `memory.json` in your folder). The
**Memory** button shows them, and any can be removed there.

The Program pane's **History** button shows earlier versions of the programs
(one is kept after each request and each save) and can put one back;
**Share** makes a link anyone can open to play the program in their browser.
Hosted, the workbench's administrators also get an **Admin** page (see
`deploy/README.md`). `PROJECTS.md` has the details.

Key presses bring the Fuse window to the front and anything you type then
lands in the emulator; everything else works with Fuse in the background.

## Limits

- One emulator, so one conversation at a time, on this machine only.
- Between conversations only the files and the agent's notes (Memory) are kept.
- `SPECDEV_MODEL` chooses the model; otherwise Claude Code's default is used.
