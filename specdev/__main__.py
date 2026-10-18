"""Spectrum coding agent with a web front end.

    python -m specdev [--port 8766]
"""

from __future__ import annotations

import argparse
import os


def main() -> int:
    parser = argparse.ArgumentParser(prog="specdev", description="Write and debug ZX Spectrum programs with an agent.")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--host", default=os.environ.get("SPECDEV_HOST", "127.0.0.1"),
                        help="address to listen on (default 127.0.0.1: this machine only)")
    args = parser.parse_args()

    from .server import serve

    serve(args.port, args.host)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
