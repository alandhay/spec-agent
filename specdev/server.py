"""Web front end for the Spectrum coding agent.

    python -m specdev            then open http://127.0.0.1:8766

On your own machine it listens on 127.0.0.1 and there is one person, "local".
Hosted (see deploy/), the environment names a Cognito user pool: every request
must then carry a sign-in token, and each person gets a workbench of their own -
their projects, an emulator and a conversation.

Projects, the library, hand-written programs and uploads are described in
PROJECTS.md. The page polls for the agent's events; the emulator's screen and
sound are long responses that the page reads as they arrive.
"""

from __future__ import annotations

import array
import base64
import binascii
import hmac
import json
import os
import queue
import re
import threading
import time
import urllib.parse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import bench as workbench
from . import mcp
from .agent import Session
from .projects import SOURCE_TYPES, UNNAMED, Library

STATIC = Path(__file__).resolve().parent / "static"
DOWNLOADS = (".tap", ".sna", ".z80", ".bin")
IMAGES = {".png": "image/png", ".jpg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
MAX_BODY = 9 * 1024 * 1024              # a request with an uploaded picture in it
MAX_IMAGE = 6 * 1024 * 1024
COOKIE = "specdev_token"
FRAME_S = 0.1 if workbench.WINDOWS else 0.04     # how often the screen stream looks for a new picture

# Hosted settings; all unset on your own machine.
POOL = os.environ.get("SPECDEV_COGNITO_POOL", "")             # e.g. us-east-1_AbCdEf
CLIENT = os.environ.get("SPECDEV_COGNITO_CLIENT", "")
AUTH_DOMAIN = os.environ.get("SPECDEV_COGNITO_DOMAIN", "")    # e.g. name.auth.us-east-1.amazoncognito.com
BUCKET = os.environ.get("SPECDEV_BUCKET", "") if POOL else ""  # where the library of projects is kept
DATA = Path(os.environ.get("SPECDEV_DATA") or workbench.ROOT / "data")
STARTING_BUDGET = float(os.environ.get("SPECDEV_STARTING_BUDGET", "10"))  # dollars a new person can spend
TOP_UPS = (5, 10, 20)                                         # dollars a person can add at a time
MAX_RUNNING = int(os.environ.get("SPECDEV_MAX_RUNNING", "6"))  # requests being worked on at once
IDLE_S = float(os.environ.get("SPECDEV_IDLE_MINUTES", "30")) * 60
MCP_KEY = os.environ.get("SPECDEV_MCP_KEY", "")                # the assistants' way in is /mcp/<this>; unset = none
MCP_USER = os.environ.get("SPECDEV_MCP_USER", "") or "local"  # the person assistants act as
PUBLIC_URL = os.environ.get("SPECDEV_URL", "").rstrip("/")    # how the outside world reaches this server
ACTIVITY = os.environ.get("SPECDEV_ACTIVITY", "")             # touched while in use; the instance stops when it goes stale

# What the agent is told about an uploaded picture, by what the person said it is.
UPLOADS = {
    "listing": "The image is a photo or scan of a program listing. Type it in exactly as a .bas file, keeping its "
               "line numbers (apply the source-format rules: keywords in upper case, variables in lower case), run "
               "it, fix what the emulator rejects, and say plainly which parts you could not read for certain.",
    "picture": "Make a ZX Spectrum line-art version of this picture. Study its main shapes and outlines, then write a "
               "BASIC program that draws them with PLOT, DRAW and CIRCLE on the 256x176 screen. Run it, take a "
               "screenshot, compare it with the original, and refine it until the subject is recognisable.",
}


class Refused(Exception):
    """Something the person asked for cannot be done; the message is for them."""


def in_use() -> None:
    if ACTIVITY:
        try:
            Path(ACTIVITY).touch()
        except OSError:
            pass


class Workspace:
    """One person's workbench: their projects, an emulator and the conversation of the current project."""

    def __init__(self, user: str) -> None:
        self.hosted = bool(POOL)
        self.home = DATA / (user if POOL else "local")
        self.home.mkdir(parents=True, exist_ok=True)
        self.library = Library(self.home, BUCKET, f"library/{user}")
        project = self.library.current
        self.bench = workbench.Bench(project.programs)
        self.session = Session(self.bench, gate=self._gate, on_done=self._record, on_event=self._keep,
                               events=project.events(), resume=project.meta["session"])
        self.edited: set[str] = set()       # files changed by hand since the agent last worked
        self.active = time.time()
        self.asleep = False
        self._usage_lock = threading.Lock()
        self._cpu: tuple[float, float] | None = None
        self._load_main()

    # -- spending -----------------------------------------------------------
    # Hosted, each person has a budget: what they have been granted (a starting amount plus
    # top-ups) less what their requests have cost. account.json holds the two totals and
    # log.jsonl every request and top-up behind them.

    def _account(self) -> dict:
        try:
            return json.loads((self.home / "account.json").read_text())
        except (OSError, ValueError):
            pass
        try:                                # before budgets there was a tally of spending per day
            earlier = json.loads((self.home / "usage.json").read_text()).values()
        except (OSError, ValueError):
            earlier = []
        return {"granted": STARTING_BUDGET, "spent": round(sum(v for v in earlier if isinstance(v, (int, float))), 4)}

    def _change(self, entry: dict, *, granted: float = 0, spent: float = 0) -> None:
        with self._usage_lock:
            account = self._account()
            account["granted"] = round(account["granted"] + granted, 4)
            account["spent"] = round(account["spent"] + spent, 4)
            (self.home / "account.json").write_text(json.dumps(account))
            with open(self.home / "log.jsonl", "a", encoding="utf-8") as log:
                log.write(json.dumps(entry) + "\n")

    def budget(self) -> dict | None:
        """What this person has left to spend; None where there are no budgets (your own machine)."""
        if not self.hosted:
            return None
        account = self._account()
        return {**account, "remaining": round(account["granted"] - account["spent"], 4), "top_ups": TOP_UPS}

    def top_up(self, amount: float) -> None:
        # In a real service this is the moment to charge the customer, and to add the
        # amount only once the payment has gone through. Here it is simply granted.
        self._change({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "top_up": amount}, granted=amount)

    def _gate(self) -> str | None:
        budget = self.budget()
        if budget and budget["remaining"] <= 0:
            return "Your budget is used up. Top up to carry on."
        if sum(w.session.running for w in list(WORKSPACES.values())) >= MAX_RUNNING:
            return "The workbench is busy with other people's requests. Try again in a minute."
        return None

    # -- the current project ---------------------------------------------------

    def _keep(self, event: dict) -> None:
        self.library.current.add_event({k: v for k, v in event.items() if k != "n"})

    def _record(self, request: dict) -> None:
        """A request finished: note it in the project, copy the project to the library, charge for it."""
        project = self.library.current
        project.meta.update(updated=time.time(), cost=project.meta.get("cost", 0) + request["cost"],
                            session=request.get("session") or project.meta["session"],
                            main=self.bench.last_run or project.meta["main"])
        png = self.bench.view()
        if png:
            (project.folder / "thumb.png").write_bytes(png)
        project.save()
        self.library.publish(project)
        if self.hosted:
            self._change({**request, "project": project.id}, spent=request["cost"])

    def _load_main(self) -> None:
        """Put the project's program on the emulator, without holding anything up."""
        main = self.library.current.meta["main"]
        if main and (self.bench.programs / main).is_file():
            def load() -> None:
                try:
                    self.bench.run(main, 0)
                except Exception:
                    pass                    # it will show when the person runs it
            threading.Thread(target=load, daemon=True, name="specdev-load").start()

    def switch(self, project_id: str | None) -> None:
        """Make another project (or, with None, a new one) the current one."""
        if self.session.running:
            raise Refused("The agent is still working. Stop it, or wait for it to finish, before changing project.")
        if project_id is not None and project_id not in self.library.projects:
            raise Refused("That project is not in your library.")
        project = self.library.new() if project_id is None else self.library.select(project_id)
        self.bench.blank()
        self.bench.programs, self.bench.last_run = project.programs, None
        self.edited.clear()
        self.session.open(project.events(), project.meta["session"])
        self._load_main()

    def say(self, text: str, image: dict | None, kind: str) -> None:
        """Pass a request, perhaps with a picture, to the agent."""
        project, told, extra = self.library.current, text, {}
        if image:
            name = self._store_image(image)
            extra["image"] = name
            told += (f"\n\n[The person attached a picture: {name}, in your working folder. Use Read on it to see it.]"
                     f"\n{UPLOADS.get(kind, '')}")
        if self.edited:
            told += (f"\n\n[Since your last turn the person changed these files by hand: {', '.join(sorted(self.edited))}. "
                     "Read them again before changing them.]")
            self.edited.clear()
        if project.meta["name"] == UNNAMED:
            label = text or {"listing": "Program from a listing", "picture": "Picture to draw"}.get(kind, "Picture")
            project.meta["name"] = " ".join(label.split())[:48]
            project.save()
        self.session.send(told, shown=text, **extra)

    def _store_image(self, image: dict) -> str:
        suffix = {mime: ext for ext, mime in IMAGES.items()}.get(image.get("type"))
        try:
            data = base64.b64decode(image.get("data") or "", validate=True)
        except (binascii.Error, ValueError):
            data = b""
        if not suffix or not data:
            raise Refused("That is not a picture this can use: send a PNG, JPEG, GIF or WebP image.")
        if len(data) > MAX_IMAGE:
            raise Refused("That picture is too big (6 MB at most).")
        programs = self.bench.programs
        name = f"upload-{len(list(programs.glob('upload-*'))) + 1}{suffix}"
        (programs / name).write_bytes(data)
        return name

    def save_file(self, name: str, text: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}\.(bas|asm|inc)", name):
            raise Refused("Name the file with letters, digits, - or _ and end it .bas, .asm or .inc.")
        if self.session.running:
            raise Refused("The agent is working on this project. Stop it, or wait, before saving your own changes.")
        (self.bench.programs / name).write_text(text.replace("\r\n", "\n"), encoding="utf-8", newline="\n")
        self.edited.add(name)
        project = self.library.current
        project.meta["updated"] = time.time()
        project.save()
        self.library.publish(project)

    def run_file(self, name: str) -> str:
        if self.session.running:
            raise Refused("The agent is using the emulator. Stop it, or wait for it to finish.")
        screen = self.bench.run(name, 3)         # long enough to see a quick program finish
        project = self.library.current
        project.meta["main"] = name
        project.save()
        return screen

    def info(self) -> dict:
        project = self.library.current
        return {"id": project.id, "name": project.meta["name"]}

    def files(self) -> list[dict]:
        files = [p for p in self.bench.programs.glob("*")
                 if p.is_file() and p.suffix.lower() in SOURCE_TYPES + DOWNLOADS]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return [{"name": p.name, "modified": p.stat().st_mtime, "size": p.stat().st_size,
                 "source": p.suffix.lower() in SOURCE_TYPES} for p in files]

    def stats(self) -> dict:
        """How hard the machine and this person's emulator are working (Linux; little is known elsewhere)."""
        out: dict = {"sessions": len(WORKSPACES), "working": sum(w.session.running for w in list(WORKSPACES.values()))}
        try:
            memory = dict(line.split(":") for line in Path("/proc/meminfo").read_text().splitlines())
            total, free = (int(memory[k].split()[0]) / 1024 for k in ("MemTotal", "MemAvailable"))
            out.update(memory_used_mb=round(total - free), memory_total_mb=round(total),
                       load=float(Path("/proc/loadavg").read_text().split()[0]), cores=os.cpu_count())
            pid = self.bench.emulator_pid
            if pid:
                fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
                ticks, now = (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK"), time.monotonic()
                if self._cpu and now > self._cpu[1]:
                    out["emulator_cpu"] = round(100 * max(0.0, ticks - self._cpu[0]) / (now - self._cpu[1]), 1)
                self._cpu = (ticks, now)
                out["emulator_mb"] = round(int(fields[21]) * os.sysconf("SC_PAGE_SIZE") / 1048576, 1)
        except (OSError, ValueError, KeyError, IndexError, AttributeError):
            pass
        return out


WORKSPACES: dict[str, Workspace] = {}
_workspaces_lock = threading.Lock()


def workspace(user: str) -> Workspace:
    with _workspaces_lock:
        if user not in WORKSPACES:
            WORKSPACES[user] = Workspace(user)
        return WORKSPACES[user]


def _janitor() -> None:
    """Put idle workbenches to sleep: their emulator and agent process are given back."""
    while True:
        time.sleep(60)
        if any(ws.session.running for ws in list(WORKSPACES.values())):
            in_use()
        for ws in list(WORKSPACES.values()):
            if not ws.asleep and not ws.session.running and time.time() - ws.active > IDLE_S:
                ws.asleep = True
                ws.session.sleep()
                ws.bench.close()


# -- sign-in ------------------------------------------------------------------

_jwks = None
_verified: dict[str, tuple[str, float]] = {}      # token -> (user, expiry), so the signature is checked once


def user_of(token: str) -> str | None:
    """The signed-in person a Cognito ID token belongs to, or None if it is not a good one."""
    global _jwks
    known = _verified.get(token)
    if known:
        return known[0] if known[1] > time.time() else None
    import jwt

    if _jwks is None:
        region = POOL.split("_")[0]
        _jwks = jwt.PyJWKClient(f"https://cognito-idp.{region}.amazonaws.com/{POOL}/.well-known/jwks.json")
    try:
        claims = jwt.decode(token, _jwks.get_signing_key_from_jwt(token).key, algorithms=["RS256"], audience=CLIENT,
                            issuer=f"https://cognito-idp.{POOL.split('_')[0]}.amazonaws.com/{POOL}")
    except Exception:
        return None
    user = str(claims.get("sub", ""))
    if claims.get("token_use") != "id" or not re.fullmatch(r"[0-9a-f-]{36}", user):
        return None
    if len(_verified) > 1000:
        _verified.clear()
    _verified[token] = (user, float(claims["exp"]))
    return user


class Handler(BaseHTTPRequestHandler):
    server_version = "specdev"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    def end_headers(self) -> None:
        if self.path.startswith("/mcp/"):   # read by the assistants' UI, which lives on another origin
            self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept, Mcp-Session-Id, Mcp-Protocol-Version")
        self.send_header("Content-Length", "0")
        self.end_headers()

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

    def _workspace(self) -> Workspace | None:
        """The caller's workbench; None (after answering 401) if they are not signed in."""
        if not POOL:
            return workspace("local")
        morsel = SimpleCookie(self.headers.get("Cookie", "")).get(COOKIE)
        user = user_of(morsel.value) if morsel else None
        if user is None:
            self._json(401, {"error": "sign in"})
            return None
        return workspace(user)

    def _assistant(self, path: str) -> tuple[Workspace, list[str]] | None:
        """For /mcp/<key>/...: the workbench assistants use and the rest of the path; None after answering 404."""
        parts = path.split("/")
        if not MCP_KEY or len(parts) < 3 or not hmac.compare_digest(parts[2].encode(), MCP_KEY.encode()):
            self._json(404, {"error": "not found"})
            return None
        ws = workspace(MCP_USER)
        ws.active, ws.asleep = time.time(), False
        in_use()
        return ws, parts[3:]

    def _stream(self, frames) -> None:
        """A response that lasts as long as the page listens: each item is a 4-byte length, then its bytes.

        ``frames`` yields bytes to send, or None when there is nothing new; an empty item
        is sent every couple of seconds so that the connection is not taken for dead.
        """
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        sent = time.monotonic()
        try:
            for frame in frames:
                if frame:
                    self.wfile.write(len(frame).to_bytes(4, "big") + frame)
                    sent = time.monotonic()
                elif time.monotonic() - sent > 2:
                    self.wfile.write(bytes(4))
                    sent = time.monotonic()
                self.wfile.flush()
        except OSError:
            pass                                        # the page went away
        finally:
            frames.close()

    @staticmethod
    def _screen(ws: Workspace):
        last = None
        while True:
            png = ws.bench.view()
            yield png if png and png != last else None
            last = png or last
            time.sleep(FRAME_S)

    @staticmethod
    def _sound(ws: Workspace):
        """The emulator's sound: first a line describing the samples, then the samples, silence left out.

        To keep it light it is sent as one channel at half the rate: every other sample of the left channel.
        """
        step = workbench.SOUND_FROM["channels"] * 2
        yield json.dumps({"rate": workbench.SOUND_FROM["rate"] // 2, "channels": 1}).encode()
        tap = listener = None
        try:
            while True:
                if ws.bench.audio is not tap:           # the emulator was started or replaced
                    if tap:
                        tap.leave(listener)
                    tap = ws.bench.audio
                    listener = tap.listen() if tap else None
                try:
                    chunk = listener.get(timeout=0.25) if listener else time.sleep(0.25)
                except queue.Empty:
                    chunk = None
                chunk = array.array("h", chunk)[0::step].tobytes() if chunk else b""
                yield chunk if chunk.count(chunk[:2]) * 2 < len(chunk) else None
        finally:
            if tap:
                tap.leave(listener)

    def do_GET(self) -> None:
        url = urllib.parse.urlsplit(self.path)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        if url.path in ("/", "/index.html"):
            return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
        if url.path.startswith("/mcp/"):
            found = self._assistant(url.path)
            if found is None:
                return
            ws, rest = found
            base = f"{PUBLIC_URL}/mcp/{MCP_KEY}"
            if rest == ["screen.stream"]:
                return self._stream(self._screen(ws))
            if rest == ["sound.stream"]:
                return self._stream(self._sound(ws))
            if rest == ["state"]:
                return self._json(200, mcp.state(ws, base))
            wanted = mcp.download(ws, rest[1], urllib.parse.unquote(rest[2])) if len(rest) == 3 and rest[0] == "file" else None
            if wanted is None:
                return self._json(405 if not rest else 404, {"error": "post JSON-RPC messages here"})
            return self._send(200, wanted[0], "application/octet-stream", download=wanted[1])
        if url.path == "/api/config":
            return self._json(200, {"auth": {"domain": AUTH_DOMAIN, "client": CLIENT, "cookie": COOKIE} if POOL else None})
        ws = self._workspace()
        if ws is None:
            return
        try:
            if url.path == "/api/events":
                snapshot = ws.session.snapshot(int(query.get("generation", 0)), int(query.get("after", 0)))
                return self._json(200, {**snapshot, "budget": ws.budget(), "project": ws.info()})
            if url.path == "/api/screen.stream":
                return self._stream(self._screen(ws))
            if url.path == "/api/sound.stream":
                return self._stream(self._sound(ws))
            if url.path == "/api/screen.png":
                png = ws.bench.view()
                return self._send(200, png, "image/png") if png else self._send(204, b"", "image/png")
            if url.path == "/api/stats":
                return self._json(200, ws.stats())
            if url.path == "/api/files":
                return self._json(200, ws.files())
            if url.path == "/api/projects":
                return self._json(200, {"current": ws.library.current.id, "projects": ws.library.summaries()})
            if url.path == "/api/thumb":
                project = ws.library.projects.get(query.get("id", ""))
                if project and (project.folder / "thumb.png").is_file():
                    return self._send(200, (project.folder / "thumb.png").read_bytes(), "image/png")
            if url.path == "/api/file":
                path = ws.bench.path(query.get("name", ""))
                suffix = path.suffix.lower()
                if path.is_file() and suffix in SOURCE_TYPES:
                    return self._send(200, path.read_bytes(), "text/plain; charset=utf-8")
                if path.is_file() and suffix in IMAGES:
                    return self._send(200, path.read_bytes(), IMAGES[suffix])
                if path.is_file() and suffix in DOWNLOADS:
                    return self._send(200, path.read_bytes(), "application/octet-stream", download=path.name)
        except (ValueError, workbench.BenchError):
            return self._json(400, {"error": "bad request"})
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        # Only this page may post: a form on another site cannot send JSON without a preflight.
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            self.close_connection = True
            return self._json(415, {"error": "send JSON"})
        if length > MAX_BODY:
            self.close_connection = True
            return self._json(400, {"error": "too much to send at once"})
        raw = self.rfile.read(length)
        if path.startswith("/mcp/"):
            found = self._assistant(path)
            if found is None:
                return
            try:
                message = json.loads(raw)
            except ValueError:
                return self._json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "bad JSON"}})
            base = f"{PUBLIC_URL}/mcp/{MCP_KEY}"
            answers = [a for a in (mcp.handle(found[0], m, base) for m in (message if isinstance(message, list) else [message])
                                   if isinstance(m, dict)) if a]
            if not answers:
                return self._send(202, b"", "application/json")
            return self._json(200, answers if isinstance(message, list) else answers[0])
        ws = self._workspace()
        if ws is None:
            return
        try:
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self._json(400, {"error": "bad JSON"})
        ws.active, ws.asleep = time.time(), False
        in_use()
        reply: dict = {"ok": True}
        try:
            if path == "/api/message":
                text, image = str(body.get("text", "")).strip(), body.get("image")
                if not text and not image:
                    raise Refused("Say what you want built.")
                ws.say(text, image if isinstance(image, dict) else None, str(body.get("kind", "")))
            elif path == "/api/stop":
                ws.session.stop()
            elif path == "/api/project/new":
                ws.switch(None)
            elif path == "/api/project/open":
                ws.switch(str(body.get("id", "")))
            elif path == "/api/file":
                ws.save_file(str(body.get("name", "")), str(body.get("text", "")))
            elif path == "/api/run":
                reply["screen"] = ws.run_file(str(body.get("name", "")))
            elif path == "/api/reset":
                ws.bench.reset()
            elif path == "/api/keys":
                keys = body.get("keys")
                if not isinstance(keys, list) or not 1 <= len(keys) <= 4:
                    raise Refused("Which keys?")
                ws.bench.keys([str(key) for key in keys], bool(body.get("down")))
            elif path == "/api/topup":
                if not ws.hosted or body.get("amount") not in TOP_UPS:
                    raise Refused("That amount cannot be added.")
                ws.top_up(body["amount"])
                reply["budget"] = ws.budget()
            else:
                return self._json(404, {"error": "not found"})
        except (Refused, workbench.BenchError) as exc:
            return self._json(400, {"error": str(exc)})
        self._json(200, reply)


def serve(port: int = 8766, host: str = "127.0.0.1") -> None:
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    in_use()
    threading.Thread(target=_janitor, daemon=True, name="specdev-janitor").start()
    print(f"specdev is running at http://{host}:{port}  (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopped")
    finally:
        for ws in list(WORKSPACES.values()):
            ws.bench.close()
