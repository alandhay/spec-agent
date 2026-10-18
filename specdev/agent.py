"""The coding agent: one conversation with Claude, with the workbench as its tools.

The agent runs through the Claude Agent SDK (which drives the installed Claude
Code, so it uses that login). It has no shell: it can read files, write inside
programs/, and use the emulator tools below. Everything it says and does is
appended to ``Session.events`` for the web page to show.
"""

from __future__ import annotations

import asyncio
import base64
import os
import shlex
import shutil
import threading
import time
import warnings
from pathlib import Path
from typing import Any, Callable

from claude_agent_sdk import (
    AssistantMessage,
    CanUseToolShadowedWarning,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
    tool,
)

from specbot.errors import SpecbotError

from . import debugger, sandbox
from .bench import Bench, BenchError
from .memory import Memory

PROMPT = (Path(__file__).resolve().parent / "prompt.md").read_text(encoding="utf-8")
SERVER = "spectrum"
FILE_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep"]
_OPEN = object()
_SLEEP = object()
LIVE_TAIL = 4000                    # characters of a part-written block sent to the page
THINKING_TOKENS = int(os.environ.get("SPECDEV_THINKING_TOKENS", "4000"))   # 0 = no visible reasoning

# Intended: only the file tools go through the callback.
warnings.filterwarnings("ignore", category=CanUseToolShadowedWarning)


def _schema(required: list[str], **properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": required}


def make_tools(bench: Bench, memory: Memory | None = None) -> list:
    async def call(fn, *args) -> dict[str, Any]:
        try:
            result = await asyncio.to_thread(fn, *args)
        except (BenchError, SpecbotError) as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "is_error": True}
        if isinstance(result, bytes):
            return {"content": [{"type": "image", "data": base64.b64encode(result).decode(), "mimeType": "image/png"}]}
        return {"content": [{"type": "text", "text": result}]}

    seconds = {"type": "number", "description": "How long to wait for a BASIC report, in seconds."}

    @tool("run_program",
          "Restart the emulator with a program from programs/ (a .bas source file is converted to a tape first and "
          "auto-runs from its first line), wait for its BASIC report, and return the screen text and the report.",
          _schema(["file"],
                  file={"type": "string", "description": "File name inside programs/, e.g. snake.bas"},
                  wait_seconds={**seconds, "default": 60},
                  code={"type": "array", "items": {"type": "string"},
                        "description": 'Machine-code blocks to add to the tape for LOAD "" CODE, each FILE@ADDRESS, '
                                       "e.g. game.bin@32768"}))
    async def run_program(args):
        return await call(bench.run, args["file"], args.get("wait_seconds", 60), args.get("code") or [])

    @tool("read_screen",
          "Return the 24 rows of text on the emulator screen and the BASIC report, if there is one.",
          _schema([], wait_seconds={**seconds, "default": 0}))
    async def read_screen(args):
        return await call(bench.screen_text, args.get("wait_seconds", 0))

    @tool("screenshot", "Return a picture of the emulator screen (colours, graphics, layout).", _schema([]))
    async def screenshot(args):
        return await call(bench.screenshot)

    @tool("press_keys",
          "Press Spectrum keys one after another, then return the screen text. Brings the emulator window to the front.",
          _schema(["keys"],
                  keys={"type": "array", "items": {"type": "string"},
                        "description": 'Key names; join a chord with +, e.g. ["P", "CAPS_SHIFT+SPACE", "ENTER"]'},
                  hold_frames={"type": "integer", "description": "Frames (1/50 s) to hold each key; default 3."}))
    async def press_keys(args):
        return await call(bench.press, args["keys"], args.get("hold_frames"))

    @tool("type_text",
          "Type literal text on the Spectrum keyboard (\\n is ENTER), then return the screen text. "
          "Brings the emulator window to the front.",
          _schema(["text"], text={"type": "string"}))
    async def type_text(args):
        return await call(bench.type_text, args["text"])

    @tool("assemble",
          "Assemble a Z80 source file in programs/ with sjasmplus into a raw .bin beside it.",
          _schema(["file"], file={"type": "string", "description": "File name inside programs/, e.g. game.asm"}))
    async def assemble(args):
        return await call(bench.assemble, args["file"])

    shows = {"type": "array", "items": {"type": "string"},
             "description": 'Values to show at each hit: registers (A, HL, IX, SP, AF\'...), "byte X" or "word X" '
                            "for memory at a label or address, or expressions of these (e.g. \"word ptr+2\", \"HL-DE\")."}

    @tool("debug_program",
          "Run a program like run_program, but with breakpoints in its machine code (Fuse's debugger). At each "
          "breakpoint hit the values asked for are recorded and the program carries on; returns a line per hit, "
          "then the screen and BASIC report. Labels come from the .sym file the assembler wrote. Use it to see "
          "registers and memory inside a routine instead of guessing from the screen.",
          _schema(["file", "breakpoints"],
                  file={"type": "string", "description": "The .bas loader (or a tape) in programs/"},
                  code={"type": "array", "items": {"type": "string"},
                        "description": 'Machine-code blocks for LOAD "" CODE, each FILE@ADDRESS, as for run_program'},
                  breakpoints={"type": "array", "maxItems": 10, "items": {"type": "object", "properties": {
                      "at": {"type": "string", "description": "Label or address of the instruction to stop at, e.g. "
                                                                "\"main_loop\", \"draw+3\", \"0x8010\""},
                      "show": shows,
                      "when": {"type": "string", "description": "Only count hits when this holds, e.g. \"B==3\", "
                                                                  "\"byte lives==0\""},
                      "times": {"type": "integer", "description": "Hits to record (default 5, at most 50)"},
                      "write": {"type": "boolean", "description": "Instead stop when the program writes to this "
                                                                  "address (a watch)"}},
                      "required": ["at"]}},
                  wait_seconds={**seconds, "default": 30}))
    async def debug_program(args):
        points = [debugger.Breakpoint(at=str(b.get("at", "")), show=[str(s) for s in b.get("show") or []],
                                      when=str(b.get("when") or ""), times=int(b.get("times") or 5),
                                      write=bool(b.get("write"))) for b in args.get("breakpoints") or []]
        return await call(bench.debug, args["file"], points, args.get("wait_seconds", 30), args.get("code") or [])

    tools = [run_program, read_screen, screenshot, press_keys, type_text, assemble, debug_program]
    if memory is None:
        return tools

    @tool("remember",
          "Keep a short note about this person for future conversations (all their projects): a preference, a goal, "
          "or a lesson worth not learning twice. One fact per note, under 300 characters.",
          _schema(["note"], note={"type": "string"}))
    async def remember(args):
        return await call(memory.add, args["note"])

    @tool("forget", "Remove one of your notes about this person, by its number in the list in your instructions.",
          _schema(["number"], number={"type": "integer"}))
    async def forget(args):
        return await call(memory.forget, int(args["number"]))

    return [*tools, remember, forget]


def claude_cli() -> str:
    """The Claude Code the SDK would run: the copy bundled with it, else the one on the PATH."""
    import claude_agent_sdk

    bundled = Path(claude_agent_sdk.__file__).resolve().parent / "_bundled" / "claude"
    return str(bundled) if bundled.is_file() else shutil.which("claude") or "claude"


OUTSIDE = "Only files inside the programs folder can be used."


def make_guard(root: Path):
    """The checks that keep the file tools inside ``root``: (permission callback, pre-tool hook).

    Both are needed: the callback is what lets Write and Edit go ahead, but it is only
    asked about calls the CLI would otherwise prompt for, and it does not prompt for
    every read. The hook sees every call.
    """
    root = root.resolve()

    def inside(value: Any) -> bool:
        path = Path(str(value))
        path = (path if path.is_absolute() else root / path).resolve()
        return path == root or root in path.parents

    def local(pattern: Any) -> bool:
        pattern = str(pattern or "")
        return not (pattern.startswith(("/", "\\", "~")) or pattern[1:2] == ":" or ".." in pattern)

    def allowed(name: str, args: dict[str, Any]) -> bool:
        if name in ("Read", "Write", "Edit"):
            return inside(args.get("file_path", "")) and Path(str(args.get("file_path", ""))).name != ""
        if name in ("Glob", "Grep"):
            return (inside(args.get("path") or root) and local(args.get("glob"))
                    and (name == "Grep" or local(args.get("pattern"))))
        return False

    async def guard(name: str, args: dict[str, Any], context: Any):
        if name not in FILE_TOOLS:
            return PermissionResultDeny(message=f"{name} is not available here.")
        return PermissionResultAllow() if allowed(name, args) else PermissionResultDeny(message=OUTSIDE)

    async def hook(data: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
        if data.get("tool_name") in FILE_TOOLS and not allowed(data["tool_name"], data.get("tool_input") or {}):
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": OUTSIDE}}
        return {}

    return guard, hook


class Session:
    """The conversation, running on its own thread; safe to use from request threads."""

    def __init__(self, bench: Bench, *, gate: Callable[[], str | None] | None = None,
                 on_done: Callable[[dict], None] | None = None,
                 on_event: Callable[[dict], None] | None = None,
                 events: list[dict] | None = None, resume: str | None = None,
                 memory: Memory | None = None,
                 on_result: Callable[[str, str], dict | None] | None = None) -> None:
        self.bench = bench
        self.memory = memory
        self._on_result = on_result         # (tool name, tool use id) -> more fields for that result's event
        self._tool_names: dict[str, str] = {}
        self._on_event = on_event           # given every event as it happens, to keep the conversation
        self._gate = gate                   # says why a new request may not start, or None
        self._on_done = on_done             # given a record of each finished request (what, tools, cost)
        self._request, self._tools = "", []
        self._live: dict[int, dict] = {}    # what the agent is part-way through saying, by block number
        self._resume = resume               # the agent session to pick up again
        self._answered = False              # has the agent said anything for the current request?
        self._retry = False
        self.events: list[dict] = list(events or [])
        self.generation = 1                 # goes up when another conversation is opened, so the page starts again
        self.running = False
        self.started: float | None = None
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self.version = 0                    # goes up whenever what snapshot() returns may have changed
        self._client: ClaudeSDKClient | None = None
        self._loop = asyncio.new_event_loop()
        self._queue: asyncio.Queue = asyncio.Queue()
        threading.Thread(target=self._thread, daemon=True, name="specdev-agent").start()

    # -- called from request threads ----------------------------------------

    def _bump(self) -> None:
        """Called with the lock held: wake whoever is waiting in wait_change()."""
        self.version += 1
        self._changed.notify_all()

    def _set_running(self, running: bool) -> None:
        with self._lock:
            self.running = running
            if running:
                self.started = time.time()
            self._bump()

    def wait_change(self, seen: int, timeout: float) -> int:
        """Wait until the version differs from ``seen`` (or ``timeout`` seconds pass); return the version."""
        with self._changed:
            self._changed.wait_for(lambda: self.version != seen, timeout)
            return self.version

    def emit(self, kind: str, **data: Any) -> None:
        with self._lock:
            event = {"n": len(self.events), "kind": kind, "at": time.time(), **data}
            self.events.append(event)
            self._bump()
        if self._on_event:
            self._on_event(event)

    def snapshot(self, generation: int, after: int) -> dict:
        with self._lock:
            start = after if generation == self.generation else 0
            live = [{**block, "text": block["text"][-LIVE_TAIL:]} for _, block in sorted(self._live.items())]
            return {"generation": self.generation, "events": self.events[start:], "running": self.running,
                    "started": self.started, "busy": self.bench.busy, "live": live, "version": self.version}

    def send(self, text: str, *, shown: str | None = None, **extra: Any) -> None:
        """Ask the agent for something. ``shown`` is what the page shows when it differs from what the agent is told."""
        shown = text if shown is None else shown
        self.emit("user", text=shown, **extra)
        refusal = self._gate() if self._gate else None
        if refusal:
            return self.emit("error", text=refusal)
        self._set_running(True)
        self._loop.call_soon_threadsafe(self._queue.put_nowait, (text, shown))

    def stop(self) -> None:
        self.bench.cancel()
        if self.running and self._client is not None:
            asyncio.run_coroutine_threadsafe(self._client.interrupt(), self._loop)

    def open(self, events: list[dict], resume: str | None) -> None:
        """Turn to another conversation: its events so far and the agent session that goes with it."""
        self._loop.call_soon_threadsafe(self._queue.put_nowait, (_OPEN, events, resume))

    def sleep(self) -> None:
        """Let go of the agent process; the next message picks the conversation up again."""
        if not self.running:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, _SLEEP)

    # -- the agent thread ---------------------------------------------------

    def _thread(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._main())

    def _launcher(self) -> str:
        """A script that starts Claude Code confined to this person's agent folder and current project.

        It lives in the person's folder, where the confined process cannot change it, and
        gives the process a home of its own, so no one's agent can read another's records.
        """
        home = self.bench.home
        agent_home = home / "agent"
        (agent_home / "tmp").mkdir(parents=True, exist_ok=True)
        cmd = sandbox.command([claude_cli()], write=[agent_home, self.bench.programs],
                              env={"HOME": str(agent_home), "TMPDIR": str(agent_home / "tmp")})
        script = home / "agent.sh"
        script.write_text(f'#!/bin/sh\nexec {shlex.join(cmd)} "$@"\n')
        script.chmod(0o755)
        return str(script)

    def _options(self) -> ClaudeAgentOptions:
        server = create_sdk_mcp_server(SERVER, tools=make_tools(self.bench, self.memory))
        guard, hook = make_guard(self.bench.programs)
        notes = (self.memory.prompt() if self.memory else
                 "\n\n(There is no memory in this conversation: the remember and forget tools are not available.)")
        return ClaudeAgentOptions(
            system_prompt=PROMPT + notes,
            cli_path=self._launcher() if sandbox.enabled() else None,
            model=os.environ.get("SPECDEV_MODEL") or None,
            cwd=str(self.bench.programs),
            tools=FILE_TOOLS,
            allowed_tools=[f"mcp__{SERVER}"],
            can_use_tool=guard,
            hooks={"PreToolUse": [HookMatcher(matcher="|".join(FILE_TOOLS), hooks=[hook])]},
            resume=self._resume,
            include_partial_messages=True,
            max_thinking_tokens=THINKING_TOKENS or None,
            mcp_servers={SERVER: server},
            strict_mcp_config=True,
            setting_sources=[],
        )

    async def _drop_client(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass

    async def _main(self) -> None:
        while True:
            item = await self._queue.get()
            if item is _SLEEP:
                await self._drop_client()
                continue
            if item[0] is _OPEN:
                await self._drop_client()
                with self._lock:
                    self.events, self._resume = list(item[1]), item[2]
                    self.generation += 1
                    self._bump()
                continue
            text, self._request = item
            self._set_running(True)
            self._tools, self._answered = [], False
            try:
                for attempt in (1, 2):
                    self._retry = False
                    try:
                        if self._client is None:
                            self.bench.programs.mkdir(parents=True, exist_ok=True)
                            self._client = ClaudeSDKClient(self._options())
                            await self._client.connect()
                        await self._client.query(text)
                        async for message in self._client.receive_response():
                            self._show(message)
                    except Exception as exc:
                        await self._drop_client()
                        if not (attempt == 1 and self._resume and not self._answered):
                            self.emit("error", text=f"{type(exc).__name__}: {exc}")
                            break
                        self._retry = True
                    if not self._retry:
                        break
                    # The saved agent session could not be picked up (its record is not on this
                    # machine): carry on in a fresh one. The files and the chat are still there.
                    await self._drop_client()
                    self._resume = None
            finally:
                with self._lock:
                    self.running = False
                    self._live = {}
                    self._bump()

    def _show_partial(self, event: dict) -> None:
        """Keep ``_live`` in step with the reply as it is written, so the page can show it growing."""
        kind, index = event.get("type"), event.get("index", 0)
        with self._lock:
            if kind == "message_start":
                self._live = {}
            elif kind == "content_block_start":
                block = event.get("content_block") or {}
                shown = {"text": "text", "thinking": "thinking", "tool_use": "tool"}.get(block.get("type"))
                if shown:
                    self._live[index] = {"kind": shown, "text": "",
                                         "name": (block.get("name") or "").removeprefix(f"mcp__{SERVER}__")}
            elif kind == "content_block_delta" and index in self._live:
                delta = event.get("delta") or {}
                self._live[index]["text"] += delta.get("text") or delta.get("thinking") or delta.get("partial_json") or ""
            else:
                return
            self._bump()

    def _finished(self, kind: str) -> None:
        """A block has arrived whole: its part-written version is no longer needed."""
        with self._lock:
            for index in sorted(self._live):
                if self._live[index]["kind"] == kind:
                    del self._live[index]
                    self._bump()
                    return

    def _show(self, message: Any) -> None:
        if isinstance(message, StreamEvent):
            if not message.parent_tool_use_id:
                self._show_partial(message.event)
        elif isinstance(message, AssistantMessage):
            self._answered = True
            for block in message.content:
                self._finished("text" if isinstance(block, TextBlock) else
                               "thinking" if isinstance(block, ThinkingBlock) else "tool")
                if isinstance(block, TextBlock) and block.text.strip():
                    self.emit("text", text=block.text)
                elif isinstance(block, ThinkingBlock) and block.thinking.strip():
                    self.emit("thinking", text=block.thinking)
                elif isinstance(block, ToolUseBlock):
                    self._tools.append(block.name.removeprefix(f"mcp__{SERVER}__"))
                    self._tool_names[block.id] = block.name.removeprefix(f"mcp__{SERVER}__")
                    self.emit("tool", id=block.id, name=block.name.removeprefix(f"mcp__{SERVER}__"), input=block.input)
        elif isinstance(message, UserMessage) and isinstance(message.content, list):
            for block in message.content:
                if isinstance(block, ToolResultBlock):
                    text, images = _result_parts(block.content)
                    name = self._tool_names.pop(block.tool_use_id, "")
                    extra = (self._on_result(name, block.tool_use_id) if self._on_result else None) or {}
                    self.emit("result", id=block.tool_use_id, text=text, images=images, error=bool(block.is_error), **extra)
        elif isinstance(message, ResultMessage):
            if message.is_error and self._resume and not self._answered and not self._retry:
                self._retry = True          # see _main: try once more without the saved session
                return
            self._resume = message.session_id or self._resume
            if self._on_done:
                self._on_done({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "request": self._request,
                               "seconds": round(message.duration_ms / 1000, 1), "turns": message.num_turns,
                               "tools": self._tools, "cost": message.total_cost_usd or 0,
                               "session": self._resume,
                               "failed": bool(message.is_error)})
            self.emit("done", seconds=round(message.duration_ms / 1000, 1), turns=message.num_turns,
                      cost=message.total_cost_usd, error=message.is_error,
                      text=(message.result or "") if message.is_error else "")


def _result_parts(content: Any) -> tuple[str, list[str]]:
    if isinstance(content, str):
        return content, []
    text, images = [], []
    for part in content or []:
        if part.get("type") == "text":
            text.append(part.get("text", ""))
        elif part.get("type") == "image":
            source = part.get("source") or {}
            data = source.get("data") or part.get("data")
            if data:
                images.append(f"data:{source.get('media_type') or part.get('mimeType') or 'image/png'};base64,{data}")
    return "\n".join(text), images
