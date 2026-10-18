"""Local web front end for specart: upload an image, watch the conversion, see it on the emulator.

    python -m specart serve            then open http://127.0.0.1:8765

Listens on 127.0.0.1 only. Uses nothing outside the standard library.
"""

from __future__ import annotations

import json
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import pipeline

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
SAMPLES = {"jupiter": pipeline.ROOT / "jupiter" / "source" / "hubble2019.jpg"}
MAX_UPLOAD = 64 * 1024 * 1024
TYPES = {".html": "text/html; charset=utf-8", ".png": "image/png", ".tap": "application/octet-stream",
         ".scr": "application/octet-stream", ".js": "text/javascript", ".css": "text/css"}
JOB_FILE = re.compile(r"^/jobs/([0-9a-f]{12})/([a-z]+\.(?:png|tap|scr))$")
JOB_STATUS = re.compile(r"^/api/jobs/([0-9a-f]{12})$")


class Handler(BaseHTTPRequestHandler):
    server_version = "specart"

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    def _send(self, code: int, body: bytes, content_type: str, download: str | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{download}"')
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, data) -> None:
        self._send(code, json.dumps(data).encode(), "application/json")

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path in ("/", "/index.html"):
            return self._send(200, (STATIC / "index.html").read_bytes(), TYPES[".html"])
        m = JOB_STATUS.match(path)
        if m:
            job = pipeline.JOBS.get(m.group(1))
            return self._json(200, job.snapshot()) if job else self._json(404, {"error": "no such job"})
        m = JOB_FILE.match(path)
        if m:
            file = pipeline.JOBS_DIR / m.group(1) / m.group(2)
            if file.is_file():
                suffix = file.suffix
                return self._send(200, file.read_bytes(), TYPES[suffix],
                                  download=file.name if suffix in (".tap", ".scr") else None)
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        url = urllib.parse.urlsplit(self.path)
        if url.path != "/api/jobs":
            return self._json(404, {"error": "not found"})
        query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        try:
            if "sample" in query:
                source = SAMPLES.get(query["sample"])
                if source is None or not source.is_file():
                    return self._json(404, {"error": "no such sample"})
                data, name = source.read_bytes(), source.name
            else:
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= MAX_UPLOAD:
                    return self._json(400, {"error": "upload an image of up to 64 MB"})
                data, name = self.rfile.read(length), query.get("name", "upload")[:120]
            job = pipeline.start_job(data, name, query.get("mode", "multicolour"), query.get("fit", "fit"),
                                     query.get("effort", "thorough"), tone=query.get("tone", "original"))
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        self._json(200, {"id": job.id})


def warm_up() -> None:
    """Compile the conversion kernels before the first upload (cached on disk afterwards)."""
    import numpy as np

    from . import convert

    target = np.zeros((convert.HEIGHT, convert.WIDTH, 3))
    heights = convert.cell_heights(True)
    indices, pairs = convert.dither(target, heights)
    convert.refine(target, indices, pairs, heights, max_passes=1)


def serve(port: int = 8765) -> None:
    threading.Thread(target=warm_up, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"specart is running at http://127.0.0.1:{port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopped")
