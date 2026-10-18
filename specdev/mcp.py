"""An MCP server for the workbench, so an assistant (Claude, ChatGPT) can use it as a tool.

It speaks MCP's streamable HTTP transport in its simplest form: each POST carries one
JSON-RPC message and gets one JSON answer; there is no server-initiated stream. The
address holds an access key (/mcp/<key>) and everything is done as one person, the
owner named in the settings: this is for demonstration, not for many users.

Requests to the agent take longer than a tool call may: `build` starts one and waits a
while; if it has not finished the assistant is told to call `check`.

The tools that show the Spectrum also name a UI (an "MCP App", static/widget.html): a
host that supports it shows the live screen, with sound, a download link for the tape
and a link to carry on at the website. The UI reads the screen and sound straight
from this server (/mcp/<key>/screen.stream and so on), which is why those answer any origin.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.parse
from pathlib import Path

VERSION = "2025-03-26"
WAIT_S = 40                     # how long one tool call waits for the agent (the web front end allows 60)
WIDGET = Path(__file__).resolve().parent / "static" / "widget.html"
# The same page under the two names hosts know it by: the MCP Apps standard, and ChatGPT's earlier form.
UI = {"ui://specdev/spectrum.html": "text/html;profile=mcp-app", "ui://specdev/spectrum-chatgpt.html": "text/html+skybridge"}
SHOWS_UI = {"ui": {"resourceUri": "ui://specdev/spectrum.html"}, "ui/resourceUri": "ui://specdev/spectrum.html",
            "openai/outputTemplate": "ui://specdev/spectrum-chatgpt.html",
            "openai/toolInvocation/invoking": "Working on the Spectrum", "openai/toolInvocation/invoked": "Here is the Spectrum"}

TOOLS = [
    {"name": "list_projects",
     "description": "List the ZX Spectrum projects in the library, newest first: id, name, program files, cost.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "open_project",
     "description": "Open a project from the library: its program is loaded into the emulator and later `build` "
                    "requests continue that project's conversation. Returns its files, download links and the screen.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string", "description": "Project id from list_projects"}},
                     "required": ["id"]}, "_meta": SHOWS_UI},
    {"name": "new_project",
     "description": "Start a new, empty project. Do this before building something unrelated to the open project.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "build",
     "description": "Ask the Spectrum coding agent to write or change a program in the open project, in plain words "
                    "(e.g. 'a bouncing ball that leaves a rainbow trail', 'make the ball faster'). It writes the "
                    "program, runs it on a real emulator and fixes it. Waits up to 40 seconds; if the agent is still "
                    "working, call `check` until it has finished. Returns what the agent said, the emulator screen "
                    "and download links for the program.",
     "inputSchema": {"type": "object", "properties": {"request": {"type": "string"}}, "required": ["request"]},
     "_meta": SHOWS_UI},
    {"name": "share",
     "description": "Make a public link to the open project's program, as last run: a web page where anyone can play "
                    "it in the browser, without signing in. Give the person the link. Calling it again updates "
                    "the same link.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "check",
     "description": "See how the open project is doing: whether the agent is still working, what it last said, the "
                    "emulator screen now, and download links for the programs.",
     "inputSchema": {"type": "object", "properties": {"wait_seconds": {"type": "number", "description": "Wait up to this long for the agent to finish (at most 40)."}}}, "_meta": SHOWS_UI},
]


def state(ws, base: str) -> dict:
    """What the UI shows beside the screen."""
    project = ws.library.current
    tapes = [f["name"] for f in ws.files() if f["name"].endswith(".tap")]
    main = (project.meta.get("main") or "").rsplit(".", 1)[0] + ".tap"
    tape = main if main in tapes else (tapes[0] if tapes else None)
    return {"name": project.meta["name"], "running": ws.session.running,
            "tape": {"name": tape, "url": f"{base}/file/{project.id}/{urllib.parse.quote(tape)}"} if tape else None}


def _resource(uri: str, base: str) -> dict:
    origin = "/".join(base.split("/")[:3])
    page = WIDGET.read_text(encoding="utf-8").replace("__BASE__", base).replace("__SITE__", origin)
    return {"uri": uri, "mimeType": UI[uri], "text": page,
            "_meta": {"ui": {"csp": {"connectDomains": [origin], "resourceDomains": [origin]}, "prefersBorder": True},
                      "openai/widgetCSP": {"connect_domains": [origin], "resource_domains": [origin]},
                      "openai/widgetPrefersBorder": True,
                      "openai/widgetDescription": "The live ZX Spectrum screen with sound, a download link and a link to the workbench."}}


def _report(ws, base: str, note: str = "") -> list[dict]:
    """What an assistant needs to show a person: state, the agent's last words, links, the screen."""
    project = ws.library.current
    running = ws.session.running
    said = [e["text"] for e in ws.session.events if e["kind"] == "text"]
    problems = [e["text"] for e in ws.session.events[-3:] if e["kind"] == "error"]
    lines = [note] if note else []
    lines.append(f"Project: {project.meta['name']} (id {project.id})")
    lines.append("The agent is still working: call `check` again in a few seconds." if running else "The agent is not working.")
    lines.append(f"Carry on at the website (live screen, sound, keyboard, editing): {'/'.join(base.split('/')[:3])}/")
    if problems:
        lines.append("Problem: " + problems[-1])
    if said:
        lines.append(("So far it has said: " if running else "The agent said: ") + said[-1])
    links = [f"{base}/file/{project.id}/{urllib.parse.quote(f['name'])}" for f in ws.files()
             if f["name"].endswith((".tap", ".bas"))]
    if links:
        lines.append("Downloads (a .tap loads in any Spectrum emulator; the .bas is the source):\n" + "\n".join(links))
    budget = ws.budget()
    if budget:
        lines.append(f"Budget left: ${max(0, budget['remaining']):.2f}")
    content: list[dict] = [{"type": "text", "text": "\n\n".join(lines)}]
    png = ws.bench.view()
    if png:
        content.append({"type": "image", "data": base64.b64encode(png).decode(), "mimeType": "image/png"})
    return content


def _wait(ws, seconds: float) -> None:
    deadline = time.monotonic() + max(0.0, min(float(seconds), WAIT_S))
    time.sleep(0.5)                     # let a request that was just sent be picked up
    while ws.session.running and time.monotonic() < deadline:
        time.sleep(0.5)


def _call(ws, base: str, name: str, args: dict) -> list[dict]:
    if name == "list_projects":
        return [{"type": "text", "text": json.dumps({"open": ws.library.current.id, "projects": ws.library.summaries()}, indent=1)}]
    if name == "open_project":
        ws.switch(str(args.get("id", "")))
        time.sleep(2.5)                 # its program is being loaded
        return _report(ws, base, "Opened.")
    if name == "new_project":
        ws.switch(None)
        return _report(ws, base, "Started a new project.")
    if name == "build":
        request = str(args.get("request", "")).strip()
        if not request:
            raise ValueError("say what to build")
        if ws.session.running:
            return _report(ws, base, "Not sent: the agent is still working on the last request.")
        ws.say(request, None, "")
        _wait(ws, WAIT_S)
        return _report(ws, base)
    if name == "share":
        site = "/".join(base.split("/")[:3])
        return [{"type": "text", "text": f"Anyone can play it here: {site}{ws.share()}"}]
    if name == "check":
        _wait(ws, args.get("wait_seconds") or 0)
        return _report(ws, base)
    raise ValueError(f"there is no tool called {name}")


def handle(ws, message: dict, base: str) -> dict | None:
    """Answer one JSON-RPC message; None for a notification, which gets no answer."""
    method, ident = message.get("method", ""), message.get("id")
    if ident is None:
        return None
    if method == "initialize":
        wanted = (message.get("params") or {}).get("protocolVersion") or VERSION
        result = {"protocolVersion": wanted, "capabilities": {"tools": {}, "resources": {}},
                  "serverInfo": {"name": "specdev", "title": "ZX Spectrum workbench", "version": "1"},
                  "instructions": "Builds and runs ZX Spectrum programs. Use list_projects/open_project to return to "
                                  "earlier work, new_project for something new, then build. Where the live "
                                  "Spectrum is not shown to the person, show them the screen image. Always give "
                                  "them the download link for the .tap and the link to the website."}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "resources/list":
        result = {"resources": [{"uri": uri, "name": "The live ZX Spectrum", "mimeType": kind} for uri, kind in UI.items()]}
    elif method == "resources/templates/list":
        result = {"resourceTemplates": []}
    elif method == "resources/read":
        uri = (message.get("params") or {}).get("uri")
        if uri not in UI:
            return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32002, "message": f"no such resource: {uri}"}}
        result = {"contents": [_resource(uri, base)]}
    elif method == "tools/call":
        params = message.get("params") or {}
        try:
            result = {"content": _call(ws, base, params.get("name", ""), params.get("arguments") or {}), "isError": False}
        except Exception as exc:        # tool problems are reported to the assistant, not as protocol errors
            result = {"content": [{"type": "text", "text": str(exc) or type(exc).__name__}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": f"unknown method {method}"}}
    return {"jsonrpc": "2.0", "id": ident, "result": result}


def download(ws, project_id: str, name: str) -> tuple[bytes, str] | None:
    """A program file from any of the person's projects, for a download link."""
    project = ws.library.projects.get(project_id)
    if project is None:
        return None
    path = (project.programs / name).resolve()
    if project.programs.resolve() not in path.parents or not path.is_file():
        return None
    if path.suffix.lower() not in (".tap", ".bas", ".asm", ".sna", ".z80"):
        return None
    return path.read_bytes(), path.name
