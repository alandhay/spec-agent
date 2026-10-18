"""Share links: a page anyone can open to play a project's program, without signing in.

Sharing copies the program's tape and a small page to play/<id>.tap and play/<id>.html,
where <id> is random and long enough not to be guessed. The page runs the tape in
JSSpeccy 3, a Spectrum emulator in the browser (GPL-3, by Matt Westcott), whose files
are kept in player/. On your own machine the server serves both folders from the data
folder; hosted, they are in the site's bucket beside the page, so a shared program
plays even while the workbench is asleep.
"""

from __future__ import annotations

import hashlib
import html
import io
import json
import secrets
import shutil
import sys
import threading
import urllib.request
import zipfile
from pathlib import Path

PLAYER_URL = "https://github.com/gasman/jsspeccy3/releases/download/v3.2/jsspeccy-3.2.zip"
PLAYER_SHA256 = "5434b5565da96a05089491f722c8fbcd8b3b70ab0277746309d2ee1db1fdbcb9"
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".wasm": "application/wasm",
                 ".tap": "application/octet-stream", ".txt": "text/plain; charset=utf-8", ".rom": "application/octet-stream", ".szx": "application/octet-stream",
                 ".bin": "application/octet-stream"}
_fetching = threading.Lock()


def player_files() -> dict[str, bytes]:
    """The emulator's files, by their path under player/, from the pinned release."""
    with urllib.request.urlopen(PLAYER_URL, timeout=60) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != PLAYER_SHA256:
        raise RuntimeError("the JSSpeccy download is not the expected file")
    files = {}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            if name.startswith("jsspeccy/") and not name.endswith("/") and not name.endswith((".DS_Store", ".map")):
                files[name.removeprefix("jsspeccy/")] = z.read(name)
        files["COPYING"] = z.read("COPYING")
    return files


def ensure_player(folder: Path) -> Path:
    """The emulator's files in ``folder``, downloaded the first time they are needed."""
    with _fetching:
        if not (folder / "jsspeccy.js").is_file():
            part = folder.with_name(folder.name + ".part")
            shutil.rmtree(part, ignore_errors=True)
            for name, data in player_files().items():
                (part / name).parent.mkdir(parents=True, exist_ok=True)
                (part / name).write_bytes(data)
            shutil.rmtree(folder, ignore_errors=True)
            part.rename(folder)
    return folder


def page(share_id: str, title: str, source_name: str | None) -> str:
    """The page that plays play/<id>.tap."""
    name = html.escape(title)
    source = (f' · <a href="{share_id}.{source_name.rsplit(".", 1)[-1]}" download="{html.escape(source_name)}">'
              f'{html.escape(source_name)}</a>' if source_name else "")
    options = json.dumps({"machine": 128, "openUrl": f"{share_id}.tap", "autoLoadTapes": True, "sandbox": True, "zoom": 2})
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name} · ZX Spectrum</title>
<script src="../player/jsspeccy.js"></script>
<style>
  body {{ margin: 0; background: #101114; color: #e8e9ec; font: 15px/1.5 system-ui, "Segoe UI", sans-serif; }}
  main {{ max-width: 720px; margin: 0 auto; padding: 20px 16px; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  p {{ color: #9aa0aa; margin: 0 0 14px; font-size: 14px; }}
  a {{ color: #ffd400; }}
  #spectrum {{ display: flex; justify-content: center; }}
  #spectrum > * {{ max-width: 100%; }}
  footer {{ color: #6b717c; font-size: 12px; margin-top: 18px; }}
</style>
</head>
<body>
<main>
  <h1>{name}</h1>
  <p>A ZX Spectrum program. Click the screen to start; it loads by itself. Your keyboard is the Spectrum's.<br>
     Download: <a href="{share_id}.tap" download="{html.escape(_file_title(title))}.tap">tape for any Spectrum emulator</a>{source}</p>
  <div id="spectrum"></div>
  <footer>Made with SpecDev. Emulator: <a href="https://github.com/gasman/jsspeccy3">JSSpeccy 3</a> by Matt Westcott
    (<a href="../player/COPYING">GPL-3</a>).</footer>
</main>
<script>JSSpeccy(document.getElementById("spectrum"), {options});</script>
</body>
</html>
"""


def _file_title(title: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in title).strip("-")[:40] or "program"


class Shares:
    """The shared programs: written to ``folder`` (served locally) and, with a bucket, to the site in S3."""

    def __init__(self, folder: Path, bucket: str = "", s3=None) -> None:
        self.folder, self.bucket, self._s3 = folder, bucket, s3

    def _client(self):
        if self._s3 is None:
            import boto3

            self._s3 = boto3.client("s3")
        return self._s3

    def publish(self, share_id: str | None, title: str, tape: bytes, source: Path | None) -> str:
        """Put a program up (again, under the same id if it has one); return the id."""
        share_id = share_id or secrets.token_urlsafe(12)
        files = {f"{share_id}.tap": tape,
                 f"{share_id}.html": page(share_id, title, source.name if source else None).encode("utf-8")}
        if source:
            files[f"{share_id}{source.suffix.lower()}"] = source.read_bytes()
        self.folder.mkdir(parents=True, exist_ok=True)
        for name in self._names(share_id):
            if name not in files:
                (self.folder / name).unlink(missing_ok=True)
        for name, data in files.items():
            (self.folder / name).write_bytes(data)
        if self.bucket:
            s3 = self._client()
            for name, data in files.items():
                s3.put_object(Bucket=self.bucket, Key=f"site/play/{name}", Body=data,
                              ContentType=CONTENT_TYPES.get(Path(name).suffix, "application/octet-stream"))
        return share_id

    def withdraw(self, share_id: str) -> None:
        names = self._names(share_id) | {f"{share_id}.{ext}" for ext in ("html", "tap", "bas", "asm", "inc")}
        for name in names:
            (self.folder / name).unlink(missing_ok=True)
        if self.bucket:
            try:
                self._client().delete_objects(Bucket=self.bucket, Delete={"Objects": [{"Key": f"site/play/{n}"} for n in names]})
            except Exception as exc:
                print(f"share: could not remove {share_id}: {exc}", file=sys.stderr, flush=True)

    def _names(self, share_id: str) -> set[str]:
        return {p.name for p in self.folder.glob(f"{share_id}.*")} if self.folder.is_dir() else set()

