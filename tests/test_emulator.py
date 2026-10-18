"""Backend-independent behaviour of the Emulator base class and the CLI parser."""

import pytest
from PIL import Image

from specbot import cli
from specbot.emulator import FRAME_S, Emulator
from specbot.keys import CAPS_SHIFT, ENTER, SYMBOL_SHIFT


class FakeEmulator(Emulator):
    def __init__(self, frames=None, **kwargs):
        self.sleeps = []
        super().__init__(sleep=self.sleeps.append, **kwargs)
        self.chords = []
        self.frames = list(frames or [Image.new("RGB", (640, 480))])
        self.closed = False

    def _capture_raw(self):
        return self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]

    def _press_chord(self, chord, hold_frames):
        self.chords.append((chord, hold_frames))

    def close(self):
        self.closed = True


def test_press_resolves_keys_and_waits_the_gap():
    emu = FakeEmulator(hold_frames=3, gap_frames=4)
    emu.press("symbol_shift", "p")
    emu.press("DELETE", hold_frames=10)
    assert emu.chords == [((SYMBOL_SHIFT, "P"), 3), ((CAPS_SHIFT, "0"), 10)]
    assert emu.sleeps == [pytest.approx(4 * FRAME_S)] * 2


def test_tap_sequence_accepts_names_tuples_and_chord_strings():
    emu = FakeEmulator()
    emu.tap_sequence(["J", ("SYMBOL_SHIFT", "P"), "SYMBOL_SHIFT+P", "ENTER"])
    assert [c for c, _ in emu.chords] == [("J",), (SYMBOL_SHIFT, "P"), (SYMBOL_SHIFT, "P"), (ENTER,)]


def test_tap_sequence_validates_before_pressing():
    emu = FakeEmulator()
    with pytest.raises(ValueError):
        emu.tap_sequence(["A", "NOPE"])
    assert emu.chords == []


def test_type_text_types_doubled_letters_as_two_presses():
    emu = FakeEmulator()
    emu.type_text("ll")
    assert [c for c, _ in emu.chords] == [("L",), ("L",)]
    assert len(emu.sleeps) == 2  # a gap after each, so the ROM sees the release


def test_type_text_rejects_unsupported_text_before_typing():
    emu = FakeEmulator()
    with pytest.raises(ValueError, match="position 3"):
        emu.type_text("abc~")
    assert emu.chords == []


def test_wait_frames():
    emu = FakeEmulator()
    emu.wait_frames(50)
    emu.wait_frames(0)
    assert emu.sleeps == [pytest.approx(1.0)]


def test_screenshot_postprocesses_and_saves(tmp_path):
    emu = FakeEmulator(crop=(0, 0, 0, 240), native_size=(320, 120))
    path = tmp_path / "out.png"
    assert emu.screenshot(str(path)).size == (320, 120)
    assert Image.open(path).size == (320, 120)
    assert emu.raw_screenshot().size == (640, 480)


def test_wait_for_change_detects_a_change():
    before = Image.new("RGB", (10, 10))
    after = Image.new("RGB", (10, 10), (255, 255, 255))
    emu = FakeEmulator(frames=[before, before, before, after])
    assert emu.wait_for_change(timeout_s=5) is True


def test_wait_for_change_times_out_and_respects_threshold():
    before = Image.new("RGB", (10, 10))
    slightly = before.copy()
    slightly.putpixel((0, 0), (255, 255, 255))  # 1% of pixels: not *more* than 1%
    emu = FakeEmulator(frames=[before, slightly])
    assert emu.wait_for_change(timeout_s=0, threshold=0.01) is False
    emu = FakeEmulator(frames=[before, slightly])
    assert emu.wait_for_change(timeout_s=0, threshold=0.005) is True


def test_wait_for_change_with_explicit_baseline():
    before = Image.new("RGB", (10, 10))
    after = Image.new("RGB", (10, 10), (255, 255, 255))
    emu = FakeEmulator(frames=[after])
    assert emu.wait_for_change(timeout_s=0, baseline=before) is True


def test_context_manager_closes():
    with FakeEmulator() as emu:
        pass
    assert emu.closed


# -- CLI ---------------------------------------------------------------------


def test_cli_parses_all_commands_and_global_flags_in_either_position():
    parser = cli.build_parser()
    args = parser.parse_args(["--backend", "zrcp", "screenshot", "out.png", "--title", "Fuse", "-v"])
    assert (args.command, args.path, args.backend, args.title, args.verbose) == (
        "screenshot", "out.png", "zrcp", "Fuse", True,
    )
    assert parser.parse_args(["press", "J", "SYMBOL_SHIFT+P"]).keys == ["J", "SYMBOL_SHIFT+P"]
    assert parser.parse_args(["type", 'LOAD ""\\n']).text == 'LOAD ""\\n'
    assert parser.parse_args(["wait", "50"]).frames == 50
    assert parser.parse_args(["calibrate"]).command == "calibrate"


def test_cli_overrides_only_include_given_flags():
    parser = cli.build_parser()
    args = parser.parse_args(
        ["--crop", "1", "2", "3", "4", "--keymap", "SYMBOL_SHIFT=alt", "wait", "1", "--port", "10001"]
    )
    assert cli._overrides(args) == {
        "crop": [1, 2, 3, 4],
        "port": 10001,
        "keymap": {"SYMBOL_SHIFT": "alt"},
    }


def test_cli_help_lists_all_commands(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for command in ("screenshot", "press", "type", "wait", "calibrate"):
        assert command in out


def test_cli_unescape():
    assert cli._unescape('LOAD ""\\n') == 'LOAD ""\n'
    assert cli._unescape("a\\\\nb") == "a\\nb"
    assert cli._unescape("plain") == "plain"


def test_cli_reports_errors_on_one_line_with_nonzero_exit(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SPECBOT_CONFIG", raising=False)
    assert cli.main(["--config", "missing.toml", "wait", "1"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("specbot: error: ")
    assert err.count("\n") == 1
