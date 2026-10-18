"""zrcp backend against a fake TCP server that mimics ZEsarUX's prompt/response format."""

import shlex
import socket
import threading

import pytest
from PIL import Image

from specbot.config import load_config
from specbot.errors import ZrcpError
from specbot.zrcp_backend import ZrcpEmulator

BANNER = "Welcome to ZEsarUX remote command protocol (ZRCP)\nWrite help for available commands\n"
PROMPT = "\ncommand> "
SCREEN_COLOUR = (0, 0, 215)


class FakeZesarux:
    def __init__(self):
        self.commands = []
        self.key_events = []
        self.screen = Image.new("RGB", (352, 288), SCREEN_COLOUR)
        self._server = socket.create_server(("127.0.0.1", 0))
        self.port = self._server.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        try:
            conn, _ = self._server.accept()
        except OSError:
            return
        with conn:
            conn.sendall((BANNER + PROMPT).encode())
            buf = b""
            while True:
                try:
                    data = conn.recv(4096)
                except OSError:
                    return
                if not data:
                    return
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    cmd = line.decode()
                    self.commands.append(cmd)
                    if cmd == "quit":
                        return
                    # Split the reply across packets to exercise prompt detection.
                    reply = (self._handle(cmd) + PROMPT).encode()
                    conn.sendall(reply[:3])
                    conn.sendall(reply[3:])

    def _handle(self, cmd):
        name, _, rest = cmd.partition(" ")
        args = shlex.split(rest, posix=False)
        if name == "send-keys-event":
            if len(args) < 2:
                return "ERROR. Needs two parameters minimum"
            self.key_events.append((int(args[0]), int(args[1])))
            return ""
        if name == "save-screen":
            if not args:
                return "ERROR. No parameter set"
            self.screen.save(args[0].strip('"'))
            return ""
        if name == "get-version":
            return "12.1"
        return "Unknown command. Write help for available commands"

    def close(self):
        self._server.close()


@pytest.fixture
def server():
    srv = FakeZesarux()
    yield srv
    srv.close()


@pytest.fixture
def sleeps():
    return []


def connect(server, sleeps, **overrides):
    cfg = load_config(backend="zrcp", port=server.port, **overrides)
    return ZrcpEmulator(cfg, sleep=sleeps.append)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SPECBOT_CONFIG", raising=False)


def test_send_returns_response_without_prompt(server, sleeps):
    with connect(server, sleeps) as emu:
        assert emu._send("get-version") == "12.1"
        assert emu._send("get-version") == "12.1"
    assert server.commands[:2] == ["get-version", "get-version"]


def test_error_responses_raise(server, sleeps):
    with connect(server, sleeps) as emu:
        with pytest.raises(ZrcpError, match="Needs two parameters"):
            emu._send("send-keys-event 97")
        with pytest.raises(ZrcpError, match="Unknown command"):
            emu._send("frobnicate")


def test_chord_presses_modifier_first_and_releases_it_last(server, sleeps):
    with connect(server, sleeps) as emu:
        emu.press("SYMBOL_SHIFT", "P")
    assert server.key_events == [(138, 1), (ord("p"), 1), (ord("p"), 0), (138, 0)]


def test_type_text_sends_expected_key_codes(server, sleeps):
    with connect(server, sleeps) as emu:
        emu.type_text("A 1\n")
    assert server.key_events == [
        (133, 1), (ord("a"), 1), (ord("a"), 0), (133, 0),
        (128, 1), (128, 0),
        (ord("1"), 1), (ord("1"), 0),
        (129, 1), (129, 0),
    ]


def test_keycodes_can_be_overridden(server, sleeps):
    with connect(server, sleeps, keycodes={"SYMBOL_SHIFT": 137}) as emu:
        emu.press("SYMBOL_SHIFT+P")
    assert server.key_events[0] == (137, 1)


def test_keys_released_after_interrupt(server):
    calls = []

    def interrupted_sleep(seconds):
        calls.append(seconds)
        if len(calls) == 2:
            raise KeyboardInterrupt

    cfg = load_config(backend="zrcp", port=server.port)
    emu = ZrcpEmulator(cfg, sleep=interrupted_sleep)
    with pytest.raises(KeyboardInterrupt):
        emu.press("CAPS_SHIFT", "A")
    emu.close()
    assert server.key_events == [(133, 1), (ord("a"), 1), (ord("a"), 0), (133, 0)]


def test_screenshot_loads_bmp_and_removes_temp_file(server, sleeps):
    with connect(server, sleeps) as emu:
        img = emu.screenshot()
    assert img.size == (352, 288)
    assert img.mode == "RGB"
    assert img.getpixel((10, 10)) == SCREEN_COLOUR
    save_cmd = next(c for c in server.commands if c.startswith("save-screen"))
    path = save_cmd.split(" ", 1)[1].strip('"')
    assert path.endswith(".bmp")
    import os

    assert not os.path.exists(path)


def test_screenshot_applies_crop_and_native_size(server, sleeps, tmp_path):
    with connect(server, sleeps, crop=[16, 24, 16, 24], native_size=[160, 120]) as emu:
        out = tmp_path / "shot.png"
        img = emu.screenshot(str(out))
        assert emu.raw_screenshot().size == (352, 288)
    assert img.size == (160, 120)
    assert Image.open(out).size == (160, 120)


def test_connection_refused_gives_clear_error(sleeps):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]
    cfg = load_config(backend="zrcp", port=free_port)
    with pytest.raises(ZrcpError, match="--enable-remoteprotocol"):
        ZrcpEmulator(cfg)
