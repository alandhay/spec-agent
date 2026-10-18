"""specdev without an emulator or the agent: memory, the events stream, the sandbox wrapper, the file guard."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("claude_agent_sdk")

from specdev import sandbox  # noqa: E402
from specdev.agent import FILE_TOOLS, Session, make_guard  # noqa: E402
from specdev.bench import Bench  # noqa: E402
from specdev.memory import MAX_CHARS, MAX_NOTES, Memory  # noqa: E402
from specdev.server import event_frames  # noqa: E402

# -- memory ---------------------------------------------------------------------


def test_memory_keeps_notes_across_instances(tmp_path):
    path = tmp_path / "memory.json"
    assert Memory(path).add("Likes games with sound") == "Remembered as note 1."
    assert Memory(path).add("  Wants   comments in the code ") == "Remembered as note 2."
    assert [n["text"] for n in Memory(path).notes()] == ["Likes games with sound", "Wants comments in the code"]


def test_memory_refuses_empty_long_duplicate_and_overflow(tmp_path):
    memory = Memory(tmp_path / "memory.json")
    assert "empty" in memory.add("   ")
    assert "too long" in memory.add("x" * (MAX_CHARS + 1))
    memory.add("one")
    assert "already" in memory.add("one")
    for i in range(MAX_NOTES - 1):
        memory.add(f"note {i}")
    assert "full" in memory.add("one too many")
    assert len(memory.notes()) == MAX_NOTES


def test_memory_forget_by_number(tmp_path):
    memory = Memory(tmp_path / "memory.json")
    for text in ("a", "b", "c"):
        memory.add(text)
    assert memory.forget(2) == "Forgot note 2: b"
    assert [n["text"] for n in memory.notes()] == ["a", "c"]
    assert "no note 5" in memory.forget(5)


def test_memory_prompt_lists_notes(tmp_path):
    memory = Memory(tmp_path / "memory.json")
    assert "Nothing yet" in memory.prompt()
    memory.add("Only uses 48K BASIC")
    assert "1. Only uses 48K BASIC" in memory.prompt()


def test_memory_survives_a_damaged_file(tmp_path):
    (tmp_path / "memory.json").write_text("{not json")
    memory = Memory(tmp_path / "memory.json")
    assert memory.notes() == []
    assert memory.add("fresh start") == "Remembered as note 1."


# -- the session's change signal and the events stream ------------------------------


def test_session_wait_change_wakes_on_emit(tmp_path):
    session = Session(Bench(tmp_path / "programs"))
    seen = session.snapshot(1, 0)["version"]
    threading.Timer(0.05, lambda: session.emit("text", text="hello")).start()
    started = time.monotonic()
    assert session.wait_change(seen, 5) != seen
    assert time.monotonic() - started < 2
    assert session.wait_change(session.version, 0.05) == session.version     # nothing new: times out


class FakeSession:
    def __init__(self):
        self.events, self.generation, self.version, self.running = [], 1, 0, False

    def snapshot(self, generation, after):
        start = after if generation == self.generation else 0
        return {"generation": self.generation, "events": self.events[start:], "running": self.running,
                "started": None, "busy": "", "live": [], "version": self.version}

    def wait_change(self, seen, timeout):
        return self.version

    def add(self, kind):
        self.events.append({"n": len(self.events), "kind": kind})
        self.version += 1


class FakeWorkspace:
    def __init__(self):
        self.session, self.listed = FakeSession(), []

    def budget(self):
        return None

    def info(self):
        return {"id": "p1", "name": "Test"}

    def files(self):
        return self.listed


def frames(stream, count):
    return [json.loads(f) if f else None for f in (next(stream) for _ in range(count))]


def test_event_frames_send_only_what_changed():
    ws = FakeWorkspace()
    ws.session.add("user")
    stream = event_frames(ws, 0, 0, idle_s=0, settle_s=0)
    first, quiet = frames(stream, 2)
    assert [e["kind"] for e in first["events"]] == ["user"] and first["generation"] == 1
    assert quiet is None
    ws.session.add("text")
    (second,) = frames(stream, 1)
    assert [e["kind"] for e in second["events"]] == ["text"]
    ws.listed = [{"name": "snake.bas"}]
    (third,) = frames(stream, 1)
    assert third["events"] == [] and third["files"] == [{"name": "snake.bas"}]
    ws.session.running = True
    (fourth,) = frames(stream, 1)
    assert fourth["running"] is True


def test_event_frames_start_again_for_another_project():
    ws = FakeWorkspace()
    ws.session.add("user")
    ws.session.add("text")
    stream = event_frames(ws, 1, 2, idle_s=0, settle_s=0)
    (first,) = frames(stream, 1)
    assert first["events"] == []
    ws.session.events, ws.session.generation = [], 2
    ws.session.add("user")
    (fresh,) = frames(stream, 1)
    assert fresh["generation"] == 2 and [e["n"] for e in fresh["events"]] == [0]
    ws.session.add("done")
    (more,) = frames(stream, 1)
    assert [e["n"] for e in more["events"]] == [1]


# -- the sandbox wrapper ------------------------------------------------------------


def test_sandbox_off_leaves_commands_alone(monkeypatch):
    monkeypatch.delenv("SPECDEV_SANDBOX", raising=False)
    assert sandbox.command(["sjasmplus", "x.asm"], write=["/tmp"]) == ["sjasmplus", "x.asm"]


def test_sandbox_on_wraps_commands(monkeypatch):
    monkeypatch.setattr(sandbox, "enabled", lambda: True)
    cmd = sandbox.command(["fuse-sdl", "--machine", "128"], read=["/roms"], write=["/data/p"], network=False,
                          env={"HOME": "/data/p"})
    assert cmd[:3] == [sys.executable, "-I", str(Path(sandbox.__file__).resolve())]
    assert cmd[3:] == ["--read", "/roms", "--write", "/data/p", "--no-network", "--set", "HOME=/data/p",
                       "--", "fuse-sdl", "--machine", "128"]


def test_sandbox_needs_a_command(capsys):
    assert sandbox.main(["--write", "/tmp"]) == 2
    assert sandbox.main(["--bogus", "--", "true"]) == 2


@pytest.mark.skipif(sys.platform.startswith("linux"), reason="elsewhere than Linux there is no Landlock")
def test_sandbox_fails_closed_without_landlock(capsys):
    assert sandbox.abi() == 0
    assert sandbox.main(["--", "true"]) == 126
    assert "cannot confine" in capsys.readouterr().err


@pytest.mark.skipif(not sys.platform.startswith("linux") or sandbox.abi() < 1, reason="needs Linux with Landlock")
def test_sandbox_confines_on_linux():
    assert sandbox._check() == 0


# -- the guard on the agent's file tools -------------------------------------------------


def decide(root: Path, name: str, args: dict) -> bool:
    guard, hook = make_guard(root)
    allowed = type(asyncio.run(guard(name, args, None))).__name__ == "PermissionResultAllow"
    if name in FILE_TOOLS:              # the hook leaves other tools to the callback
        hooked = asyncio.run(hook({"tool_name": name, "tool_input": args}, None, None))
        assert allowed == (hooked == {}), "the callback and the hook disagree"
    return allowed


def test_guard_keeps_file_tools_in_the_folder(tmp_path):
    root = tmp_path / "programs"
    root.mkdir()
    assert decide(root, "Write", {"file_path": str(root / "snake.bas")})
    assert decide(root, "Read", {"file_path": "snake.bas"})
    assert not decide(root, "Read", {"file_path": str(tmp_path / "account.json")})
    assert not decide(root, "Edit", {"file_path": str(root / ".." / "memory.json")})
    assert decide(root, "Glob", {"pattern": "*.bas"})
    assert not decide(root, "Glob", {"pattern": "../*"})
    assert not decide(root, "Grep", {"pattern": "x", "path": str(tmp_path)})
    assert not decide(root, "Bash", {"command": "ls"})
