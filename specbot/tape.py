"""Turn Sinclair BASIC source text (and raw code blocks) into a .tap file.

Source format, one BASIC line per text line::

    10 REM keywords are UPPER CASE, variables lower case
    20 FOR n=1 TO 10: PRINT n,n*n: NEXT n

* Every line starts with its line number. Blank lines and lines starting
  with ``#`` are ignored.
* Keywords must be upper case (``GOTO`` / ``GOSUB`` are accepted for
  ``GO TO`` / ``GO SUB``); anything in lower case is left alone, so variables
  such as ``score`` or ``at`` are safe.
* Escapes (zmakebas-compatible), mostly for use inside strings:
  ``\\a``..``\\u`` UDGs, ``\\{N}`` any character code (``\\{0x10}`` works too),
  ``\\*`` the copyright sign, ``\\\\`` a backslash, and block graphics as a
  backslash plus two of ``space ' . :`` (left half, right half), e.g. ``\\::``
  is a solid block and ``\\'.`` is top-left + bottom-right.
"""

from __future__ import annotations

import math
import re
import struct
from typing import Iterable

TOKENS = [
    "SPECTRUM", "PLAY",  # 0xA3, 0xA4 (128K only)
    "RND", "INKEY$", "PI", "FN", "POINT", "SCREEN$", "ATTR", "AT", "TAB", "VAL$",
    "CODE", "VAL", "LEN", "SIN", "COS", "TAN", "ASN", "ACS", "ATN", "LN", "EXP",
    "INT", "SQR", "SGN", "ABS", "PEEK", "IN", "USR", "STR$", "CHR$", "NOT", "BIN",
    "OR", "AND", "<=", ">=", "<>", "LINE", "THEN", "TO", "STEP", "DEF FN", "CAT",
    "FORMAT", "MOVE", "ERASE", "OPEN #", "CLOSE #", "MERGE", "VERIFY", "BEEP",
    "CIRCLE", "INK", "PAPER", "FLASH", "BRIGHT", "INVERSE", "OVER", "OUT",
    "LPRINT", "LLIST", "STOP", "READ", "DATA", "RESTORE", "NEW", "BORDER",
    "CONTINUE", "DIM", "REM", "FOR", "GO TO", "GO SUB", "INPUT", "LOAD", "LIST",
    "LET", "PAUSE", "NEXT", "POKE", "PRINT", "PLOT", "RUN", "SAVE", "RANDOMIZE",
    "IF", "CLS", "DRAW", "CLEAR", "RETURN", "COPY",
]
TOKEN_CODES = {name: 0xA3 + i for i, name in enumerate(TOKENS)}
assert TOKEN_CODES["RND"] == 0xA5 and TOKEN_CODES["COPY"] == 0xFF

_SPELLINGS = dict(TOKEN_CODES)
_SPELLINGS.update(
    {
        "GOTO": TOKEN_CODES["GO TO"],
        "GOSUB": TOKEN_CODES["GO SUB"],
        "DEFFN": TOKEN_CODES["DEF FN"],
        "OPEN#": TOKEN_CODES["OPEN #"],
        "CLOSE#": TOKEN_CODES["CLOSE #"],
    }
)
# Longest first so INKEY$ beats IN, VAL$ beats VAL, and so on.
_BY_LENGTH = sorted(_SPELLINGS, key=len, reverse=True)

_NUMBER_RE = re.compile(r"(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?")
_BLOCK_BITS_LEFT = {" ": 0, "'": 2, ".": 8, ":": 10}
_BLOCK_BITS_RIGHT = {" ": 0, "'": 1, ".": 4, ":": 5}
_CHAR_CODES = {"£": 0x60, "©": 0x7F}

NO_AUTOSTART = 0x8000


class BasicError(ValueError):
    """The BASIC source could not be converted."""


def encode_number(value: float) -> bytes:
    """The hidden 5-byte form the ROM stores after every numeric literal."""
    if value == int(value) and 0 <= value <= 65535:
        n = int(value)
        return bytes([0, 0, n & 0xFF, n >> 8, 0])
    if value <= 0:
        raise BasicError(f"cannot encode number {value!r}")
    exponent = math.floor(math.log2(value)) + 1
    mantissa = round(value / 2.0**exponent * 2**32)
    if mantissa >= 2**32:
        mantissa >>= 1
        exponent += 1
    if not -127 <= exponent <= 127:
        raise BasicError(f"number {value!r} is out of range")
    return bytes([0x80 + exponent]) + struct.pack(">I", mantissa & 0x7FFFFFFF)


def _escape(text: str, i: int, where: str) -> tuple[bytes, int]:
    """Decode the backslash escape at text[i]; return (bytes, next index)."""
    rest = text[i + 1 :]
    if rest[:1] == "{":
        end = rest.find("}")
        try:
            code = int(rest[1:end], 0) if end > 0 else -1
        except ValueError:
            code = -1
        if not 0 <= code <= 255:
            raise BasicError(f"{where}: bad character code escape {text[i:i + end + 2]!r}")
        return bytes([code]), i + end + 2
    if rest[:1] == "\\":
        return b"\\", i + 2
    if rest[:1] == "*":
        return b"\x7f", i + 2
    if len(rest) >= 2 and rest[0] in _BLOCK_BITS_LEFT and rest[1] in _BLOCK_BITS_RIGHT:
        return bytes([0x80 + _BLOCK_BITS_LEFT[rest[0]] + _BLOCK_BITS_RIGHT[rest[1]]]), i + 3
    if rest[:1].lower() in "abcdefghijklmnopqrstu" and rest[:1]:
        return bytes([0x90 + ord(rest[0].lower()) - ord("a")]), i + 2
    raise BasicError(f"{where}: unknown escape {text[i:i + 2]!r}")


def _char(ch: str, where: str) -> bytes:
    if ch in _CHAR_CODES:
        return bytes([_CHAR_CODES[ch]])
    if 32 <= ord(ch) < 127:
        return ch.encode("ascii")
    raise BasicError(f"{where}: character {ch!r} is not in the Spectrum character set")


def _match_keyword(text: str, i: int) -> str | None:
    prev = text[i - 1] if i else " "
    for word in _BY_LENGTH:
        if not text.startswith(word, i):
            continue
        if word[0].isalpha():
            if prev.isalpha():
                continue  # part of a longer word
            nxt = text[i + len(word) : i + len(word) + 1]
            if word[-1].isalpha() and (nxt.isupper() or nxt == "$" and word + "$" in _SPELLINGS):
                continue
        return word
    return None


def tokenize_statement_text(text: str, where: str = "line") -> bytes:
    """Tokenize the part of a BASIC line after its line number."""
    out = bytearray()
    i, n = 0, len(text)
    in_string = False
    def_fn_params = False  # inside the parameter list of DEF FN
    while i < n:
        ch = text[i]
        if ch == "\\":
            data, i = _escape(text, i, where)
            out += data
            continue
        if in_string:
            in_string = ch != '"'
            out += _char(ch, where)
            i += 1
            continue
        if ch == '"':
            in_string = True
            out += b'"'
            i += 1
            continue

        word = _match_keyword(text, i)
        if word:
            code = _SPELLINGS[word]
            if out.endswith(b" ") and word[0].isalpha():
                out.pop()  # the token prints its own leading space
            out.append(code)
            i += len(word)
            if text[i : i + 1] == " ":
                i += 1
            if code == TOKEN_CODES["REM"]:
                while i < n:  # the rest of the line is literal
                    if text[i] == "\\":
                        data, i = _escape(text, i, where)
                        out += data
                    else:
                        out += _char(text[i], where)
                        i += 1
                break
            if code == TOKEN_CODES["BIN"]:
                m = re.compile(r" *([01]+)").match(text, i)
                if not m:
                    raise BasicError(f"{where}: BIN must be followed by binary digits")
                out += m.group(1).encode() + b"\x0e" + encode_number(int(m.group(1), 2))
                i = m.end()
            if code == TOKEN_CODES["DEF FN"]:
                def_fn_params = True
            continue

        if def_fn_params:
            # The ROM keeps a 5-byte value slot after each DEF FN parameter.
            if ch in ",)" and out[-1:].isalpha() or ch in ",)" and out[-1:] == b"$":
                out += b"\x0e\x00\x00\x00\x00\x00"
            if ch == "=":
                def_fn_params = False

        prev = text[i - 1] if i else " "
        if (ch.isdigit() or ch == ".") and not (prev.isalnum() or prev == "_"):
            m = _NUMBER_RE.match(text, i)
            if m:
                out += m.group(0).encode() + b"\x0e" + encode_number(float(m.group(0)))
                i = m.end()
                continue
        out += _char(ch, where)
        i += 1
    if in_string:
        raise BasicError(f"{where}: unterminated string")
    return bytes(out)


def tokenize_program(source: str) -> bytes:
    """Convert BASIC source text into the program bytes stored on tape."""
    program = bytearray()
    last = 0
    for lineno, raw in enumerate(source.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"(\d+)\s*(.*)", line)
        if not m:
            raise BasicError(f"source line {lineno}: missing BASIC line number: {line[:30]!r}")
        number = int(m.group(1))
        where = f"BASIC line {number}"
        if not 1 <= number <= 9999:
            raise BasicError(f"source line {lineno}: line number {number} must be 1-9999")
        if number <= last:
            raise BasicError(f"source line {lineno}: line number {number} is not above {last}")
        if not m.group(2):
            raise BasicError(f"{where}: empty line")
        last = number
        body = tokenize_statement_text(m.group(2), where) + b"\r"
        program += struct.pack(">H", number) + struct.pack("<H", len(body)) + body
    if not program:
        raise BasicError("no BASIC lines found")
    return bytes(program)


def _block(flag: int, data: bytes) -> bytes:
    payload = bytes([flag]) + data
    checksum = 0
    for b in payload:
        checksum ^= b
    return struct.pack("<H", len(payload) + 1) + payload + bytes([checksum])


def _file(kind: int, name: str, data: bytes, param1: int, param2: int) -> bytes:
    name_bytes = name.encode("ascii", "replace")[:10].ljust(10)
    header = bytes([kind]) + name_bytes + struct.pack("<HHH", len(data), param1, param2)
    return _block(0x00, header) + _block(0xFF, data)


def program_tap(program: bytes, name: str = "program", autostart: int | None = None) -> bytes:
    """A tape file holding a BASIC program (as ``SAVE name LINE autostart``)."""
    start = NO_AUTOSTART if autostart is None else autostart
    return _file(0, name, program, start, len(program))


def code_tap(data: bytes, address: int, name: str = "code") -> bytes:
    """A tape file holding a block of bytes (as ``SAVE name CODE address,len``)."""
    if not 0 <= address <= 65535 or address + len(data) > 65536:
        raise BasicError(f"code block {name!r} does not fit in memory at {address}")
    return _file(3, name, data, address, 32768)


def basic_to_tap(
    source: str,
    name: str = "program",
    autostart: int | None = None,
    code_blocks: Iterable[tuple[bytes, int]] = (),
) -> bytes:
    """Build a complete .tap image: the BASIC program, then any CODE blocks.

    Load the code blocks from the program with ``LOAD "" CODE``.
    """
    tap = program_tap(tokenize_program(source), name, autostart)
    for index, (data, address) in enumerate(code_blocks, 1):
        tap += code_tap(data, address, f"code{index}")
    return tap
