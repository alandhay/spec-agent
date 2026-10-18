"""Breakpoints for the agent: run a program in Fuse with its debugger scripted, and report what was seen.

Fuse takes debugger commands at start-up (--debugger-command): breakpoints on an address
(or a memory write), each with commands that run when it is hit. Here every hit prints a
marker, the program counter and the values asked for, then carries on; Fuse writes them to
its standard output, which the workbench keeps in a file and turns back into lines like

    #1 at main_loop (0x8008), hit 1: A=42 (0x2A)  HL=1234 (0x04D2)  byte score=7 (0x07)

The agent names things the way it wrote them: registers (A, HL, IX, SP, AF', ...), labels from
the .sym file the assembler writes, numbers, and "byte X" / "word X" for memory. They are
translated to Fuse's own expressions here, and anything that cannot be is refused before the
emulator is started.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

REGISTERS = {name: f"z80:{name.lower()}" for name in
             ("A", "F", "B", "C", "D", "E", "H", "L", "AF", "BC", "DE", "HL", "IX", "IY", "SP", "PC", "I", "R")}
REGISTERS.update({f"{name}'": f"z80:{name.lower()}_" for name in ("AF", "BC", "DE", "HL")})
MAX_TIMES = 50                          # hits reported per breakpoint
MAX_SHOWN = 12                          # values shown per breakpoint
MARK = 0xD000000                        # printed first on every hit: MARK + breakpoint number

NUMBER = r"(?:0x[0-9A-Fa-f]+|\$[0-9A-Fa-f]+|[0-9A-Fa-f]+h\b|\d+)"
NAME = r"[A-Za-z_.][A-Za-z0-9_.?!@]*"


class DebugError(ValueError):
    """Something in the request that cannot be turned into a breakpoint; worded for the agent."""


def read_sym(path: Path) -> dict[str, int]:
    """Labels from an sjasmplus .sym file ("label: EQU 0x00008008")."""
    labels = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return labels
    for line in lines:
        m = re.match(rf"\s*({NAME})\s*:?\s+EQU\s+(\S+)", line, re.IGNORECASE)
        if m:
            try:
                labels[m.group(1)] = _number(m.group(2))
            except DebugError:
                pass
    return labels


def _number(text: str) -> int:
    text = text.strip()
    try:
        if text.lower().startswith("0x"):
            return int(text, 16)
        if text.startswith("$"):
            return int(text[1:], 16)
        if text.lower().endswith("h") and re.fullmatch(r"[0-9A-Fa-f]+h", text, re.IGNORECASE):
            return int(text[:-1], 16)
        return int(text)
    except ValueError:
        raise DebugError(f"{text!r} is not a number") from None


def address(text: str, labels: dict[str, int]) -> int:
    """An address written as a number, a label, or a label plus or minus a number."""
    m = re.fullmatch(rf"\s*({NAME}|{NUMBER})\s*(?:([+-])\s*({NUMBER}))?\s*", str(text))
    if not m:
        raise DebugError(f"{text!r} is not an address: give a number or a label, perhaps +N")
    base = m.group(1)
    if re.fullmatch(NUMBER, base):
        value = _number(base)
    elif base in labels:
        value = labels[base]
    else:
        raise DebugError(f"there is no label {base!r} in the .sym file" if labels else
                         f"{base!r} is not a number, and there is no .sym file to look labels up in")
    if m.group(2):
        value += _number(m.group(3)) * (1 if m.group(2) == "+" else -1)
    if not 0 <= value <= 0xFFFF:
        raise DebugError(f"{text!r} is outside the 64K address space")
    return value


def expression(text: str, labels: dict[str, int]) -> str:
    """A value or condition as the agent wrote it, as a Fuse debugger expression."""
    text = str(text).strip()
    if not text or len(text) > 120:
        raise DebugError("an expression must be 1 to 120 characters")

    def memory(m: re.Match) -> str:
        where = address(m.group(2), labels)
        return f"[{where}]" if m.group(1).lower() == "byte" else f"([{where}]+256*[{(where + 1) & 0xFFFF}])"

    out = re.sub(rf"\b(byte|word)\s+({NAME}(?:\s*[+-]\s*{NUMBER})?|{NUMBER})", memory, text, flags=re.IGNORECASE)
    parts = []
    for token in re.findall(rf"\[\d+\]|{NAME}'?|{NUMBER}|==|!=|<=|>=|&&|\|\||[-+*/%&|^<>()!~]|\s+|.", out):
        upper = token.upper()
        if token.startswith("[") or token.isspace() or re.fullmatch(r"==|!=|<=|>=|&&|\|\||[-+*/%&|^<>()!~]", token):
            parts.append(token)
        elif upper in REGISTERS:
            parts.append(REGISTERS[upper])
        elif re.fullmatch(NUMBER, token):
            parts.append(str(_number(token)))
        elif token in labels:
            parts.append(str(labels[token]))
        else:
            raise DebugError(f"cannot use {token!r} in {text!r}: use registers (A, HL, IX, SP, AF'...), labels, "
                             "numbers, 'byte X' or 'word X', and C-style operators")
    return "".join(parts)


@dataclass
class Breakpoint:
    at: str                             # an address, or for a watch the address written to
    show: list[str] = field(default_factory=list)
    when: str = ""
    times: int = 5
    write: bool = False                 # a watch: stop when the program writes to ``at``


def script(points: list[Breakpoint], labels: dict[str, int]) -> tuple[str, list[dict]]:
    """Fuse's debugger commands for these breakpoints, and what each hit's lines will mean."""
    if not points:
        raise DebugError("give at least one breakpoint or watch")
    if len(points) > 10:
        raise DebugError("at most 10 breakpoints and watches at once")
    lines, layout = [], []
    for number, point in enumerate(points, 1):
        where = address(point.at, labels)
        shows = point.show[:MAX_SHOWN]
        exprs = [expression(s, labels) for s in shows]
        times = max(1, min(int(point.times or 1), MAX_TIMES))
        condition = f"$h{number}<{times}" + (f" && ({expression(point.when, labels)})" if point.when else "")
        lines += [f"set $h{number} 0",
                  f"breakpoint {'write ' if point.write else ''}{where} if {condition}",
                  f"commands {number}",
                  f"set $h{number} $h{number}+1",
                  f"print {MARK + number}",
                  "print z80:pc",
                  *(f"print {e}" for e in exprs),
                  "continue",
                  "end"]
        name = next((k for k, v in labels.items() if v == where), None)
        layout.append({"number": number, "where": where, "name": name, "write": point.write, "shows": shows})
    return "\n".join(lines), layout


def report(output: str, layout: list[dict], labels: dict[str, int]) -> str:
    """Fuse's printed values turned back into a line per hit."""
    values = []
    for line in output.splitlines():
        line = line.strip()
        if re.fullmatch(r"0x[0-9a-fA-F]+|\d+", line):
            values.append(int(line, 0))
    by_pc = {v: k for k, v in labels.items()}
    hits, i, counts = [], 0, {}
    while i < len(values):
        number = values[i] - MARK
        if not 1 <= number <= len(layout):
            i += 1
            continue
        point = layout[number - 1]
        counts[number] = counts.get(number, 0) + 1
        got = values[i + 1:i + 2 + len(point["shows"])]
        i += 1 + len(got)
        pc = got[0] if got else None
        where = f"{point['name']} ({point['where']:#06x})" if point["name"] else f"{point['where']:#06x}"
        if point["write"]:
            head = f"#{number} write to {where}, hit {counts[number]}, PC={_hex(pc)} (at or just after the writing instruction)"
            if pc is not None and pc in by_pc:
                head += f" ({by_pc[pc]})"
        else:
            head = f"#{number} at {where}, hit {counts[number]}"
        shown = [f"{name}={_value(v)}" for name, v in zip(point["shows"], got[1:])]
        hits.append(head + (": " + "  ".join(shown) if shown else ""))
    if not hits:
        return "No breakpoint was hit."
    missed = [f"#{p['number']}" for p in layout if p["number"] not in counts]
    return "\n".join(hits) + (f"\nNot hit: {', '.join(missed)}." if missed else "")


def _hex(value: int | None) -> str:
    return "?" if value is None else f"{value:#06x}"


def _value(value: int) -> str:
    return f"{value} ({value:#04x})" if value < 256 else f"{value} ({value:#06x})"
