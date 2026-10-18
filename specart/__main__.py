"""Command line for specart.

    python -m specart serve [--port 8765]
    python -m specart convert IMAGE [--out DIR] [--mode multicolour|standard]
                                    [--fit fit|fill|stretch] [--effort quick|thorough]
                                    [--tone original|lift|lift-more] [--no-emulator]
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(prog="specart", description="Turn any image into a ZX Spectrum picture.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("serve", help="start the web front end")
    p.add_argument("--port", type=int, default=8765)
    p = sub.add_parser("convert", help="convert one image and check it on the emulator")
    p.add_argument("image")
    p.add_argument("--out", help="folder for the results (default: next to the image, NAME_spectrum)")
    p.add_argument("--mode", choices=("multicolour", "standard"), default="multicolour")
    p.add_argument("--fit", choices=("fit", "fill", "stretch"), default="fit")
    p.add_argument("--effort", choices=("quick", "thorough"), default="thorough")
    p.add_argument("--tone", choices=("original", "lift", "lift-more"), default="original",
                   help="lift dark tones before converting (the Spectrum has no dark colours)")
    p.add_argument("--no-emulator", action="store_true", help="convert only; do not run it in Fuse")
    args = parser.parse_args()

    if args.command == "serve":
        from .server import serve

        serve(args.port)
        return 0

    from . import pipeline

    source = Path(args.image)
    try:
        data = source.read_bytes()
    except OSError as exc:
        print(f"specart: error: {exc}", file=sys.stderr)
        return 1
    job = pipeline.Job(source.name, args.mode, args.fit, args.effort, use_emulator=not args.no_emulator, tone=args.tone)
    seen = 0

    def follow() -> None:
        nonlocal seen
        log = job.snapshot()["log"]
        for line in log[seen:]:
            print(line, flush=True)
        seen = len(log)

    import threading
    import time

    worker = threading.Thread(target=job.run, args=(data,), daemon=True)
    worker.start()
    while worker.is_alive():
        follow()
        time.sleep(0.3)
    follow()
    status = job.snapshot()
    if status["state"] != "done":
        print(f"specart: error: {status['error']}", file=sys.stderr)
        return 1
    out = Path(args.out) if args.out else source.with_name(source.stem + "_spectrum")
    out.mkdir(parents=True, exist_ok=True)
    for file in job.dir.iterdir():
        shutil.copy2(file, out / file.name)
    shutil.rmtree(job.dir, ignore_errors=True)
    for stage in status["stages"]:
        print(f"[{stage['state']:>7}] {stage['label']}: {stage['detail']}")
    print(f"Results in {out}")
    return 0 if status["result"].get("exact", True) else 2


if __name__ == "__main__":
    sys.exit(main())
