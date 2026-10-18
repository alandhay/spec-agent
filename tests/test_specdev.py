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


# -- the MCP server and its widget ------------------------------------------------------

from specdev import mcp  # noqa: E402

BASE = "https://example.test/mcp/key"


def rpc(method, params=None, ident=1, ws=None):
    return mcp.handle(ws, {"jsonrpc": "2.0", "id": ident, "method": method, "params": params or {}}, BASE)


def test_mcp_tools_name_the_widget():
    tools = {t["name"]: t for t in rpc("tools/list")["result"]["tools"]}
    assert set(tools) == {"list_projects", "open_project", "new_project", "build", "share", "check"}
    for name in ("build", "check", "open_project"):
        assert tools[name]["_meta"]["ui"]["resourceUri"] == "ui://specdev/spectrum.html"


def test_mcp_widget_is_filled_in_for_this_server():
    contents = rpc("resources/read", {"uri": "ui://specdev/spectrum.html"})["result"]["contents"][0]
    assert contents["mimeType"] == "text/html;profile=mcp-app"
    assert "__BASE__" not in contents["text"] and "__SITE__" not in contents["text"]
    assert f'const BASE = "{BASE}"' in contents["text"]
    assert contents["_meta"]["ui"]["csp"]["connectDomains"] == ["https://example.test"]
    assert "error" in rpc("resources/read", {"uri": "ui://elsewhere"})


def test_mcp_protocol_odds_and_ends():
    assert mcp.handle(None, {"jsonrpc": "2.0", "method": "notifications/initialized"}, BASE) is None
    assert rpc("initialize", {"protocolVersion": "2025-06-18"})["result"]["protocolVersion"] == "2025-06-18"
    assert rpc("nonsense")["error"]["code"] == -32601
    result = rpc("tools/call", {"name": "no_such_tool", "arguments": {}})["result"]
    assert result["isError"] and "no tool called" in result["content"][0]["text"]


# -- project history -------------------------------------------------------------------

from specdev import projects  # noqa: E402
from specdev.projects import Project  # noqa: E402


def test_history_keeps_changed_versions_only(tmp_path):
    project = Project(tmp_path / "p")
    assert project.keep_version("empty") is None                 # nothing to keep yet
    (project.programs / "a.bas").write_text("10 PRINT 1\n")
    (project.programs / "a.tap").write_bytes(b"tape")            # not a source file: not kept
    assert project.keep_version("first")["files"] == ["a.bas"]
    assert project.keep_version("again") is None                 # unchanged
    (project.programs / "a.bas").write_text("10 PRINT 2\n")
    assert project.keep_version("second")["n"] == 2
    assert [v["why"] for v in project.versions()] == ["first", "second"]
    assert project.version_file(1, "a.bas") == (project.programs / "a.bas").read_bytes().replace(b"2", b"1")


def test_history_restore_can_be_undone(tmp_path):
    project = Project(tmp_path / "p")
    (project.programs / "a.bas").write_text("10 PRINT 1\n")
    project.keep_version("one")
    (project.programs / "a.bas").write_text("10 PRINT 2\n")
    (project.programs / "b.asm").write_text("ret\n")
    assert project.restore(1) == ["a.bas", "b.asm"]
    assert (project.programs / "a.bas").read_text() == "10 PRINT 1\n"
    assert not (project.programs / "b.asm").exists()
    before = project.versions()[-1]
    assert before["why"] == "before going back to version 1" and before["files"] == ["a.bas", "b.asm"]
    project.restore(before["n"])
    assert (project.programs / "b.asm").read_text() == "ret\n"
    with pytest.raises(KeyError):
        project.restore(99)


def test_history_drops_the_oldest(tmp_path, monkeypatch):
    monkeypatch.setattr(projects, "MAX_VERSIONS", 3)
    project = Project(tmp_path / "p")
    for i in range(5):
        (project.programs / "a.bas").write_text(f"10 PRINT {i}\n")
        project.keep_version(str(i))
    assert [v["n"] for v in project.versions()] == [3, 4, 5]
    assert sorted(p.name for p in (project.folder / "history").iterdir()) == ["3", "4", "5"]


# -- share links ------------------------------------------------------------------------

from specdev import share  # noqa: E402


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[Key] = (Body, ContentType)

    def delete_objects(self, Bucket, Delete):
        for item in Delete["Objects"]:
            self.objects.pop(item["Key"], None)


def test_share_publishes_page_tape_and_source(tmp_path):
    source = tmp_path / "stars.bas"
    source.write_bytes(b"10 PRINT 1\n")
    s3 = FakeS3()
    shares = share.Shares(tmp_path / "shared", "bucket", s3)
    ident = shares.publish(None, "Stars <b>", b"TAPE", source)
    assert len(ident) >= 12
    assert sorted(p.name for p in (tmp_path / "shared").iterdir()) == [f"{ident}.{e}" for e in ("bas", "html", "tap")]
    page = (tmp_path / "shared" / f"{ident}.html").read_text(encoding="utf-8")
    assert "Stars &lt;b&gt;" in page and f'"openUrl": "{ident}.tap"' in page and "../player/jsspeccy.js" in page
    assert s3.objects[f"site/play/{ident}.html"][1].startswith("text/html")
    assert s3.objects[f"site/play/{ident}.tap"][0] == b"TAPE"


def test_share_again_keeps_the_link_and_withdraw_removes_it(tmp_path):
    s3 = FakeS3()
    shares = share.Shares(tmp_path / "shared", "bucket", s3)
    source = tmp_path / "a.bas"
    source.write_bytes(b"10 PRINT 1\n")
    ident = shares.publish(None, "A", b"ONE", source)
    assert shares.publish(ident, "A", b"TWO", None) == ident
    assert (tmp_path / "shared" / f"{ident}.tap").read_bytes() == b"TWO"
    assert not (tmp_path / "shared" / f"{ident}.bas").exists()      # the source was not shared this time
    shares.withdraw(ident)
    assert not any((tmp_path / "shared").iterdir()) and not s3.objects


# -- the admin side -----------------------------------------------------------------------

import datetime  # noqa: E402

from specdev import admin  # noqa: E402
from specdev.server import budget_of, change_account  # noqa: E402

ALICE = "11111111-2222-3333-4444-555555555555"
BOB = "66666666-7777-8888-9999-000000000000"


class FakeCognito:
    def __init__(self):
        self.calls = []

    def get_paginator(self, name):
        users = [{"Username": ALICE, "Enabled": True, "UserStatus": "CONFIRMED",
                  "UserCreateDate": datetime.datetime(2026, 10, 1, tzinfo=datetime.timezone.utc),
                  "Attributes": [{"Name": "sub", "Value": ALICE}, {"Name": "email", "Value": "alice@example.com"}]},
                 {"Username": BOB, "Enabled": True, "UserStatus": "FORCE_CHANGE_PASSWORD",
                  "UserCreateDate": datetime.datetime(2026, 10, 2, tzinfo=datetime.timezone.utc),
                  "Attributes": [{"Name": "sub", "Value": BOB}, {"Name": "email", "Value": "bob@example.com"}]}]
        return type("P", (), {"paginate": lambda self, **kw: [{"Users": users}]})()

    def admin_create_user(self, **kw):
        self.calls.append(("create", kw["Username"]))

    def admin_disable_user(self, **kw):
        self.calls.append(("disable", kw["Username"]))

    def admin_enable_user(self, **kw):
        self.calls.append(("enable", kw["Username"]))


def test_admin_lists_people_with_folders_and_invitations(tmp_path):
    home = tmp_path / ALICE
    home.mkdir()
    admin.seen(home, "alice@example.com")
    change_account(home, {"request": "a game", "at": "2026-10-18T10:00:00Z", "cost": 0.5}, spent=0.5)
    (home / "projects" / "p1").mkdir(parents=True)
    (tmp_path / "shared").mkdir()                      # not a person
    people = admin.people(tmp_path, admin.Pool("us-east-1_x", FakeCognito()), budget_of)
    assert [p["id"] for p in people] == [ALICE, BOB]
    alice, bob = people
    assert alice["email"] == "alice@example.com" and alice["requests"] == 1 and alice["projects"] == 1
    assert alice["account"]["spent"] == 0.5 and alice["status"] == "CONFIRMED"
    assert bob["last"] is None and bob["status"] == "FORCE_CHANGE_PASSWORD" and bob["account"] is None


def test_admin_suspend_and_seen(tmp_path):
    home = tmp_path / ALICE
    home.mkdir()
    assert not admin.suspended(home)
    admin.update_person(home, suspended=True)
    assert admin.suspended(home)
    admin.seen(home, "alice@example.com")
    assert admin.person(home)["email"] == "alice@example.com" and admin.suspended(home)


def test_admin_pool_calls(tmp_path):
    cognito = FakeCognito()
    pool = admin.Pool("us-east-1_x", cognito)
    pool.invite("carol@example.com")
    pool.enable(ALICE, False)
    pool.enable(ALICE, True)
    assert cognito.calls == [("create", "carol@example.com"), ("disable", ALICE), ("enable", ALICE)]


def test_admin_requests_through_the_server(tmp_path, monkeypatch):
    from specdev import server

    cognito = FakeCognito()
    monkeypatch.setattr(server, "DATA", tmp_path)
    monkeypatch.setattr(server, "POOL", "us-east-1_x")
    monkeypatch.setattr(server, "POOL_ADMIN", admin.Pool("us-east-1_x", cognito))
    monkeypatch.setattr(server, "ADMINS", {"boss@example.com"})
    me = type("W", (), {"is_admin": True, "email": "boss@example.com", "home": tmp_path / BOB})()
    assert server.Handler._admin(me, "/api/admin/invite", {"email": "carol@example.com"}) == {"invited": "carol@example.com"}
    with pytest.raises(server.Refused):
        server.Handler._admin(me, "/api/admin/invite", {"email": "not an address"})
    granted = server.Handler._admin(me, "/api/admin/grant", {"id": ALICE, "amount": 20})
    assert granted["account"]["granted"] == server.STARTING_BUDGET + 20
    assert admin.log_tail(tmp_path / ALICE)[0] == {**admin.log_tail(tmp_path / ALICE)[0], "grant": 20, "by": "boss@example.com"}
    with pytest.raises(server.Refused):
        server.Handler._admin(me, "/api/admin/grant", {"id": ALICE, "amount": 5000})
    with pytest.raises(server.Refused):
        server.Handler._admin(me, "/api/admin/grant", {"id": "../etc", "amount": 5})
    assert server.Handler._admin(me, "/api/admin/suspend", {"id": ALICE, "suspended": True}) == {"suspended": True}
    assert admin.suspended(tmp_path / ALICE) and ("disable", ALICE) in cognito.calls
    with pytest.raises(server.Refused):
        server.Handler._admin(me, "/api/admin/suspend", {"id": BOB, "suspended": True})      # yourself
    not_admin = type("W", (), {"is_admin": False, "email": "x@example.com", "home": tmp_path / "x"})()
    with pytest.raises(server.Refused):
        server.Handler._admin(not_admin, "/api/admin/invite", {"email": "carol@example.com"})


# -- breakpoints ----------------------------------------------------------------------------

from specdev import debugger  # noqa: E402

LABELS = {"start": 0x8000, "loop": 0x8008, "here": 0x800A, "store": 0x800B}


def test_debugger_reads_sym_files(tmp_path):
    sym = tmp_path / "x.sym"
    sym.write_text("here: EQU 0x0000800A\nstart: EQU 0x00008000\n; a comment\n")
    assert debugger.read_sym(sym) == {"here": 0x800A, "start": 0x8000}
    assert debugger.read_sym(tmp_path / "missing.sym") == {}


def test_debugger_addresses_and_expressions():
    assert debugger.address("here", LABELS) == 0x800A
    assert debugger.address("here+2", LABELS) == 0x800C
    assert debugger.address("0x5C00", LABELS) == 0x5C00 and debugger.address("$5c00", LABELS) == 0x5C00
    assert debugger.address("5C00h", LABELS) == 0x5C00 and debugger.address("23552", LABELS) == 23552
    assert debugger.expression("HL", LABELS) == "z80:hl"
    assert debugger.expression("AF'", LABELS) == "z80:af_"
    assert debugger.expression("byte store", LABELS) == "[32779]"
    assert debugger.expression("word store+1", LABELS) == "([32780]+256*[32781])"
    assert debugger.expression("B==3 && byte 0x5C00>0", LABELS) == "z80:b==3 && [23552]>0"
    for bad in ("nolabel", "system(1)", "HL; exit", "", "byte nowhere"):
        with pytest.raises(debugger.DebugError):
            debugger.expression(bad, LABELS)
    with pytest.raises(debugger.DebugError):
        debugger.address("0x10000", LABELS)


def test_debugger_script_and_report():
    points = [debugger.Breakpoint("here", ["A", "word store"]), debugger.Breakpoint("loop", ["B"], when="B<3", times=2),
              debugger.Breakpoint("store", ["PC"], write=True, times=1)]
    commands, layout = debugger.script(points, LABELS)
    assert "breakpoint 32778 if $h1<5\ncommands 1\nset $h1 $h1+1" in commands
    assert "breakpoint 32776 if $h2<2 && (z80:b<3)" in commands
    assert "breakpoint write 32779 if $h3<1" in commands
    m = debugger.MARK
    printed = "\n".join(hex(v) for v in [m + 2, 0x8008, 2, m + 2, 0x8008, 1, m + 3, 0x8006, 0x8006, m + 1, 0x800A, 42, 1234])
    seen = debugger.report("Fuse says hello\n" + printed, layout, LABELS)
    assert seen.splitlines() == [
        "#2 at loop (0x8008), hit 1: B=2 (0x02)",
        "#2 at loop (0x8008), hit 2: B=1 (0x01)",
        "#3 write to store (0x800b), hit 1, PC=0x8006 (at or just after the writing instruction): PC=32774 (0x8006)",
        "#1 at here (0x800a), hit 1: A=42 (0x2a)  word store=1234 (0x04d2)"]
    assert debugger.report("", layout, LABELS) == "No breakpoint was hit."
    assert debugger.report(hex(m + 1) + "\n0x800a\n1\n0x0", layout, LABELS).endswith("Not hit: #2, #3.")
    with pytest.raises(debugger.DebugError):
        debugger.script([], LABELS)


# -- payments -------------------------------------------------------------------------------

import hashlib  # noqa: E402
import hmac  # noqa: E402

from specdev import payments  # noqa: E402


def signed(payload: bytes, secret: str, stamp: int) -> str:
    return f"t={stamp},v1=" + hmac.new(secret.encode(), f"{stamp}.".encode() + payload, hashlib.sha256).hexdigest()


def test_stripe_checkout_asks_for_the_right_payment():
    sent = []
    stripe = payments.Stripe("sk_test_x", "whsec_y", send=lambda path, fields: sent.append((path, fields)) or {"url": "https://checkout.stripe.com/c/1"})
    url = stripe.checkout(ALICE, "alice@example.com", 10, "https://site/?paid={CHECKOUT_SESSION_ID}", "https://site/?paid=cancelled")
    assert url == "https://checkout.stripe.com/c/1" and stripe.test_mode
    path, fields = sent[0]
    assert path == "/checkout/sessions" and fields["mode"] == "payment"
    assert fields["line_items[0][price_data][unit_amount]"] == "1000" and fields["metadata[credit_cents]"] == "1000"
    assert fields["client_reference_id"] == ALICE == fields["metadata[user]"] and fields["customer_email"] == "alice@example.com"
    with pytest.raises(payments.PaymentError):
        payments.Stripe("sk_test_x", "whsec_y", send=lambda p, f: {}).checkout(ALICE, "", 5, "a", "b")


def test_stripe_webhooks_are_trusted_only_when_signed():
    stripe = payments.Stripe("sk_test_x", "whsec_y")
    body = json.dumps({"type": "checkout.session.completed"}).encode()
    now = 1_800_000_000
    assert stripe.verify(body, signed(body, "whsec_y", now), now=now)["type"] == "checkout.session.completed"
    for header in (signed(body, "wrong", now), signed(body + b" ", "whsec_y", now)[:0] + signed(b"other", "whsec_y", now), "", "v1=abc"):
        with pytest.raises(payments.PaymentError):
            stripe.verify(body, header, now=now)
    with pytest.raises(payments.PaymentError):                         # replayed long after
        stripe.verify(body, signed(body, "whsec_y", now), now=now + 3600)


def test_only_paid_sessions_give_credit():
    def event(**session):
        return {"type": "checkout.session.completed",
                "data": {"object": {"id": "cs_1", "payment_status": "paid", "amount_total": 1180,
                                    "metadata": {"user": ALICE, "credit_cents": "1000"}, **session}}}
    assert payments.credit_from(event()) == (ALICE, 10.0, "cs_1")        # the credit sold, not the total with tax
    assert payments.credit_from(event(payment_status="unpaid")) is None
    assert payments.credit_from(event(metadata={})) is None
    assert payments.credit_from({"type": "payment_intent.created"}) is None


def test_a_payment_is_credited_once(tmp_path, monkeypatch):
    from specdev import server

    monkeypatch.setattr(server, "DATA", tmp_path)
    assert server.credit(ALICE, 10.0, "cs_1") is True
    assert server.credit(ALICE, 10.0, "cs_1") is False                 # Stripe sent it again
    assert server.budget_of(tmp_path / ALICE)["granted"] == server.STARTING_BUDGET + 10
    assert admin.log_tail(tmp_path / ALICE)[0]["stripe_session"] == "cs_1"
    with pytest.raises(payments.PaymentError):
        server.credit("../etc", 10.0, "cs_2")
