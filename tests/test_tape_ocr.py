"""BASIC -> .tap conversion and screen-text reading (no emulator needed)."""

import struct

import pytest
from PIL import Image

from specbot import cli
from specbot.config import load_config
from specbot.errors import CaptureError, ConfigError
from specbot.ocr import load_font, read_screen_text
from specbot.tape import (
    BasicError,
    TOKEN_CODES,
    basic_to_tap,
    code_tap,
    encode_number,
    program_tap,
    tokenize_program,
    tokenize_statement_text as tok,
)

T = TOKEN_CODES


def num(text, value_bytes):
    return text.encode() + b"\x0e" + bytes(value_bytes)


# -- tokenizer -----------------------------------------------------------------


def test_token_table_matches_the_rom():
    assert T["RND"] == 0xA5 and T["THEN"] == 0xCB and T["PRINT"] == 0xF5 and T["COPY"] == 0xFF


def test_simple_line_layout():
    program = tokenize_program('10 PRINT "hi"')
    body = bytes([T["PRINT"]]) + b'"hi"\r'
    assert program == struct.pack(">H", 10) + struct.pack("<H", len(body)) + body


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, [0, 0, 0, 0, 0]),
        (10, [0, 0, 10, 0, 0]),
        (65535, [0, 0, 0xFF, 0xFF, 0]),
        (0.5, [0x80, 0, 0, 0, 0]),
        (0.1, [0x7D, 0x4C, 0xCC, 0xCC, 0xCD]),
        (65536, [0x91, 0, 0, 0, 0]),
        (1.5, [0x81, 0x40, 0, 0, 0]),
    ],
)
def test_encode_number(value, expected):
    assert encode_number(value) == bytes(expected)


def test_numbers_get_their_hidden_value():
    assert tok("LET a=10") == bytes([T["LET"]]) + b"a=" + num("10", [0, 0, 10, 0, 0])
    assert tok("LET a=.5") == bytes([T["LET"]]) + b"a=" + num(".5", [0x80, 0, 0, 0, 0])
    assert tok("LET a=1e3") == bytes([T["LET"]]) + b"a=" + num("1e3", [0, 0, 0xE8, 3, 0])
    # A digit inside a variable name is not a number.
    assert tok("LET a1=b2") == bytes([T["LET"]]) + b"a1=b2"


def test_keywords_need_upper_case_and_word_boundaries():
    assert tok("LET score=at+print") == bytes([T["LET"]]) + b"score=at+print"
    assert tok("FOR n=a TO b") == bytes([T["FOR"]]) + b"n=a" + bytes([T["TO"]]) + b"b"
    assert tok("PRINTa") == bytes([T["PRINT"]]) + b"a"


def test_longest_keyword_wins():
    assert tok("LET a$=INKEY$") == bytes([T["LET"]]) + b"a$=" + bytes([T["INKEY$"]])
    assert tok("INPUT a") == bytes([T["INPUT"]]) + b"a"
    assert tok("PRINT INT x") == bytes([T["PRINT"], T["INT"]]) + b"x"
    assert tok("IF a<=b THEN STOP") == (
        bytes([T["IF"]]) + b"a" + bytes([T["<="]]) + b"b" + bytes([T["THEN"], T["STOP"]])
    )


def test_goto_and_gosub_spellings():
    assert tok("GOTO 10") == tok("GO TO 10") == bytes([T["GO TO"]]) + num("10", [0, 0, 10, 0, 0])
    assert tok("GOSUB 10")[0] == T["GO SUB"]


def test_strings_and_rem_are_literal():
    assert tok('PRINT "PRINT 10"') == bytes([T["PRINT"]]) + b'"PRINT 10"'
    assert tok("REM PRINT 10 and so on") == bytes([T["REM"]]) + b"PRINT 10 and so on"


def test_bin_and_def_fn():
    assert tok("LET a=BIN 101") == bytes([T["LET"]]) + b"a=" + bytes([T["BIN"]]) + num("101", [0, 0, 5, 0, 0])
    slot = b"\x0e\x00\x00\x00\x00\x00"
    assert tok("DEF FN s(x,y$)=x") == bytes([T["DEF FN"]]) + b"s(x" + slot + b",y$" + slot + b")=x"
    # Arguments of a call are not parameters.
    assert tok("PRINT FN s(a,b)") == bytes([T["PRINT"], T["FN"]]) + b"s(a,b)"


def test_escapes_and_special_characters():
    assert tok('PRINT "\\a\\u"') == bytes([T["PRINT"]]) + b'"\x90\xa4"'
    assert tok('PRINT "\\::\\ \'\\\'."') == bytes([T["PRINT"]]) + b'"\x8f\x81\x86"'
    assert tok('PRINT "\\{22}\\{0x10}\\*\\\\"') == bytes([T["PRINT"]]) + b'"\x16\x10\x7f\\"'
    assert tok('PRINT "£"') == bytes([T["PRINT"]]) + b'"\x60"'


@pytest.mark.parametrize(
    "source,message",
    [
        ("PRINT 1", "missing BASIC line number"),
        ("20 PRINT 1\n10 PRINT 2", "not above 20"),
        ("0 PRINT 1", "must be 1-9999"),
        ('10 PRINT "oops', "unterminated string"),
        ('10 PRINT "é"', "not in the Spectrum character set"),
        ("10 PRINT \\z", "unknown escape"),
        ("10", "empty line"),
        ("# just a comment\n", "no BASIC lines"),
    ],
)
def test_source_errors(source, message):
    with pytest.raises(BasicError, match=message):
        tokenize_program(source)


def test_comments_and_blank_lines_are_skipped():
    assert tokenize_program("# header\n\n10 CLS\n") == tokenize_program("10 CLS")


# -- tape ----------------------------------------------------------------------


def blocks(tap):
    out = []
    while tap:
        (length,) = struct.unpack("<H", tap[:2])
        out.append(tap[2 : 2 + length])
        tap = tap[2 + length :]
    return out


def xor(data):
    result = 0
    for b in data:
        result ^= b
    return result


def test_program_tap_structure():
    program = tokenize_program("10 CLS\n20 GOTO 10")
    header, data = blocks(program_tap(program, "demo", autostart=10))
    assert len(header) == 19 and header[0] == 0x00 and header[1] == 0  # header flag, type Program
    assert header[2:12] == b"demo      "
    assert struct.unpack("<HHH", header[12:18]) == (len(program), 10, len(program))
    assert data[0] == 0xFF and data[1:-1] == program
    assert xor(header) == 0 and xor(data) == 0  # checksum makes each block XOR to zero


def test_program_without_autostart_and_code_block():
    header, _ = blocks(program_tap(b"\x00", "x"))
    assert struct.unpack("<H", header[14:16]) == (0x8000,)
    header, data = blocks(code_tap(b"\xc9", 32768))
    assert header[1] == 3 and struct.unpack("<HHH", header[12:18]) == (1, 32768, 32768)
    assert data[1:-1] == b"\xc9"
    with pytest.raises(BasicError, match="does not fit"):
        code_tap(b"\x00" * 10, 65530)


def test_basic_to_tap_appends_code_blocks():
    tap = basic_to_tap('10 LOAD "" CODE', code_blocks=[(b"\x00\xc9", 40000)])
    assert len(blocks(tap)) == 4


def test_cli_bas2tap_defaults_to_autostart_at_first_line(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SPECBOT_CONFIG", raising=False)
    (tmp_path / "game.bas").write_text("30 CLS\n40 STOP\n", encoding="utf-8")
    (tmp_path / "mc.bin").write_bytes(b"\xc9")
    assert cli.main(["bas2tap", "game.bas", "--code", "mc.bin@0x8000"]) == 0
    parts = blocks((tmp_path / "game.tap").read_bytes())
    assert len(parts) == 4
    assert parts[0][2:12] == b"game      "
    assert struct.unpack("<H", parts[0][14:16]) == (30,)
    assert struct.unpack("<H", parts[2][14:16]) == (0x8000,)

    assert cli.main(["bas2tap", "game.bas", "out.tap", "--no-autostart"]) == 0
    assert struct.unpack("<H", blocks((tmp_path / "out.tap").read_bytes())[0][14:16]) == (0x8000,)

    (tmp_path / "bad.bas").write_text("PRINT 1\n", encoding="utf-8")
    assert cli.main(["bas2tap", "bad.bas"]) == 1
    assert "missing BASIC line number" in capsys.readouterr().err


# -- screen text ---------------------------------------------------------------


def fake_rom(tmp_path, size=16384):
    """A ROM-sized file with a distinct made-up glyph for each character."""
    rom = bytearray(size)
    base = size - 16384 + 0x3D00
    for code in range(33, 128):  # 32 (space) stays blank
        for line in range(8):
            rom[base + (code - 32) * 8 + line] = (code * 7 + line * 29 + 1) & 0xFE | (line == 0)
    path = tmp_path / "fake.rom"
    path.write_bytes(bytes(rom))
    return path


def render(rows, rom_path, size=(320, 240), ink=(0, 0, 0), paper=(215, 215, 215), inverse=()):
    rom = rom_path.read_bytes()[-16384:]
    img = Image.new("RGB", size, paper)
    left, top = (size[0] - 256) // 2, (size[1] - 192) // 2
    for r, text in enumerate(rows):
        for c, ch in enumerate(text):
            code = {"£": 0x60, "©": 0x7F}.get(ch, ord(ch))
            glyph = rom[0x3D00 + (code - 32) * 8 : 0x3D00 + (code - 31) * 8]
            fg, bg = (paper, ink) if (r, c) in inverse else (ink, paper)
            for y in range(8):
                for x in range(8):
                    img.putpixel((left + c * 8 + x, top + r * 8 + y), fg if glyph[y] & (0x80 >> x) else bg)
    return img


def test_reads_text_with_and_without_border(tmp_path):
    rom = fake_rom(tmp_path)
    font = load_font(rom)
    rows = ["10 PRINT \"Hello\"", "", "2 Variable not found, 80:1", "£ and ©"]
    for size in ((320, 240), (256, 192)):
        text = read_screen_text(render(rows, rom, size), font)
        assert len(text) == 24
        assert text[:4] == rows
        assert text[4:] == [""] * 20


def test_reads_inverse_and_coloured_text_and_flags_graphics(tmp_path):
    rom = fake_rom(tmp_path, size=32768)  # 128K-style two-ROM image
    font = load_font(rom)
    img = render(["MENU ok"], rom, ink=(215, 215, 0), paper=(0, 0, 215), inverse={(0, 0), (0, 1)})
    for x in range(40, 43):  # three colours in one cell: not text
        img.putpixel((32 + x, 24 + 1), (x * 5, 0, 0))
    assert read_screen_text(img, font)[0] == "MENU ?k"


def test_screen_text_errors(tmp_path):
    font = load_font(fake_rom(tmp_path))
    with pytest.raises(CaptureError, match="native_size"):
        read_screen_text(Image.new("RGB", (960, 721)), font)
    (tmp_path / "short.rom").write_bytes(b"\x00" * 100)
    with pytest.raises(ConfigError, match="not a Spectrum ROM"):
        load_font(tmp_path / "short.rom")
    with pytest.raises(ConfigError, match="cannot read"):
        load_font(tmp_path / "missing.rom")


def test_rom_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SPECBOT_CONFIG", raising=False)
    assert load_config().rom_path is None
    assert load_config(rom="x.rom").rom_path == "x.rom"
    # Defaults to roms/48.rom beside the launched emulator.
    (tmp_path / "roms").mkdir()
    (tmp_path / "roms" / "48.rom").write_bytes(b"")
    (tmp_path / "specbot.toml").write_text(
        f"[launch]\npath = '{(tmp_path / 'emu.exe').as_posix()}'\n", encoding="utf-8"
    )
    assert load_config().rom_path == str(tmp_path / "roms" / "48.rom")
    with pytest.raises(ConfigError, match="rom"):
        load_config(rom=5)
