"""Check the workbench on the hosted instance without the agent: run, read, keys, assemble.

    python deploy/deploy.py run "cd /opt/specdev/app && sudo -u specdev env HOME=/var/lib/specdev SPECDEV_SANDBOX=1 /opt/specdev/venv/bin/python deploy/smoke.py"

With SPECDEV_SANDBOX=1 it also checks that the assembler cannot read a file outside its folder.
With --agent it then asks the agent for a small program (costs a few cents). That needs the hosted
settings, which only root can read: start it with
    set -a; . /etc/specdev.env; set +a; cd /opt/specdev/app && sudo -E -u specdev HOME=/var/lib/specdev /opt/specdev/venv/bin/python deploy/smoke.py --agent
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from specdev import sandbox  # noqa: E402
from specdev.bench import Bench  # noqa: E402

programs = Path(tempfile.mkdtemp()) / "programs"
programs.mkdir()
(programs / "hello.bas").write_text('10 BORDER 1: PRINT "HELLO FROM THE CLOUD"\n20 PRINT 6*7\n')
(programs / "ask.bas").write_text('10 INPUT a$\n20 PRINT "got ";a$\n30 STOP\n')
(programs / "code.asm").write_text("    org 32768\n    ld bc,1234\n    ret\n")
(programs / "code.bas").write_text('10 CLEAR 32767\n20 LOAD "" CODE\n30 PRINT USR 32768\n')
(programs / "dbg.asm").write_text("    org 32768\nstart:\n    ld a,42\n    ld hl,1234\n    ld (store),hl\n"
                                  "    ld b,3\nloop:\n    djnz loop\nhere:\n    ret\nstore:\n    dw 0\n")

bench = Bench(programs)
failed = False


def check(name: str, text: str, *wanted: str) -> None:
    global failed
    missing = [w for w in wanted if w not in text]
    failed = failed or bool(missing)
    print(f"{'ok  ' if not missing else 'FAIL'} {name}" + (f"  (missing {missing})\n{text}" if missing else ""))


try:
    started = time.monotonic()
    check("run a BASIC program", bench.run("hello.bas", 60), "HELLO FROM THE CLOUD", "42", "0 OK, 20:1")
    print(f"     took {time.monotonic() - started:.1f} s")
    png = bench.screenshot()
    check("screenshot", "png" if png[:4] == b"\x89PNG" and len(png) > 500 else "bad", "png")
    (programs / "hello.png").write_bytes(png)
    check("waits at INPUT", bench.run("ask.bas", 8), "no BASIC report")
    check("typing", bench.type_text("hi\n"), "got hi", "9 STOP statement, 30:1")
    check("assemble", bench.assemble("code.asm"), "4 bytes")
    check("machine code", bench.run("code.bas", 60, ["code.bin@32768"]), "1234", "0 OK, 30:1")
    from specdev.debugger import Breakpoint

    check("assemble with labels", bench.assemble("dbg.asm"), "bytes")
    check("breakpoints", bench.debug("code.bas", [Breakpoint("here", ["A", "HL", "word store"]),
                                                  Breakpoint("loop", ["B"], times=2)], 60, ["dbg.bin@32768"]),
          "#1 at here", "A=42 (0x2a)", "HL=1234 (0x04d2)", "word store=1234", "#2 at loop", "hit 2: B=2", "0 OK, 30:1")
    if sandbox.enabled():
        secret = Path(tempfile.mkdtemp()) / "secret.bin"
        secret.write_bytes(b"not yours")
        (programs / "peek.asm").write_text(f'    org 32768\n    incbin "{secret}"\n')
        try:
            outcome = bench.assemble("peek.asm")
        except Exception as exc:
            outcome = f"refused: {exc}"
        check("assembler confined", outcome, "refused")
    if "--agent" in sys.argv:
        from specdev.agent import Session

        session = Session(bench)
        session.send("Write count.bas that prints the numbers 1 to 5 on one line, run it, and quote the report. "
                     "Then try to Read /etc/hostname and say in one line whether that was allowed.")
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline and not any(e["kind"] in ("done", "error") for e in session.events):
            time.sleep(1)
        for e in session.events:
            print(f"     {e['kind']:8} {e.get('name', '')} {str(e.get('text') or e.get('input') or '')[-200:]!r}")
        last = session.events[-1]
        check("agent", f"{last['kind']} error={last.get('error')}", "done error=False")
        check("agent's program", (programs / "count.bas").read_text() if (programs / "count.bas").is_file() else "", "PRINT")
finally:
    bench.close()
print("FAILED" if failed else "all passed")
sys.exit(1 if failed else 0)
