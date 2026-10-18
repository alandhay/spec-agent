"""Spectrum key model and text -> keystroke mapping.

A *key* is one of the 40 physical Spectrum keys, named canonically in upper
case. A *chord* is a tuple of keys pressed together. Aliases such as ``DELETE``
resolve to chords.
"""

from __future__ import annotations

import string
from typing import Iterable, Union

CAPS_SHIFT = "CAPS_SHIFT"
SYMBOL_SHIFT = "SYMBOL_SHIFT"
ENTER = "ENTER"
SPACE = "SPACE"

MODIFIERS = (CAPS_SHIFT, SYMBOL_SHIFT)

SPECTRUM_KEYS = frozenset(
    list(string.digits) + list(string.ascii_uppercase) + [ENTER, SPACE, CAPS_SHIFT, SYMBOL_SHIFT]
)

Chord = tuple[str, ...]
KeySpec = Union[str, tuple[str, ...]]

ALIASES: dict[str, Chord] = {
    "DELETE": (CAPS_SHIFT, "0"),
    "BACKSPACE": (CAPS_SHIFT, "0"),
    "LEFT": (CAPS_SHIFT, "5"),
    "DOWN": (CAPS_SHIFT, "6"),
    "UP": (CAPS_SHIFT, "7"),
    "RIGHT": (CAPS_SHIFT, "8"),
    "EDIT": (CAPS_SHIFT, "1"),
    "BREAK": (CAPS_SHIFT, SPACE),
    "EXTEND": (CAPS_SHIFT, SYMBOL_SHIFT),
}

# Characters reached with SYMBOL_SHIFT + key (standard Spectrum layout).
SYMBOL_KEYS: dict[str, str] = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5",
    "&": "6", "'": "7", "(": "8", ")": "9", "_": "0",
    "<": "R", ">": "T", ";": "O", '"': "P", "^": "H",
    "-": "J", "+": "K", "=": "L", ":": "Z", "£": "X",
    "?": "C", "/": "V", "*": "B", ",": "N", ".": "M",
}


def _canonical(name: str) -> str:
    return name.strip().upper().replace(" ", "_").replace("-", "_")


def normalize_key(name: str) -> str:
    """Return the canonical name of a single physical Spectrum key."""
    key = _canonical(name)
    if key not in SPECTRUM_KEYS:
        raise ValueError(f"unknown Spectrum key {name!r}")
    return key


def resolve(name: str) -> Chord:
    """Resolve a key name, alias or ``A+B`` chord string to a chord."""
    if not isinstance(name, str):
        raise ValueError(f"key name must be a string, got {name!r}")
    if "+" in name:
        return resolve_chord(tuple(part for part in name.split("+")))
    key = _canonical(name)
    if key in ALIASES:
        return ALIASES[key]
    if key in SPECTRUM_KEYS:
        return (key,)
    raise ValueError(f"unknown Spectrum key {name!r}")


def resolve_chord(keys: KeySpec | Iterable[str]) -> Chord:
    """Resolve one or more key names/aliases into a single de-duplicated chord."""
    names = (keys,) if isinstance(keys, str) else tuple(keys)
    if not names:
        raise ValueError("no keys given")
    chord: list[str] = []
    for name in names:
        for key in resolve(name):
            if key not in chord:
                chord.append(key)
    return tuple(chord)


def split_chord(chord: Chord) -> tuple[Chord, Chord]:
    """Split a chord into (modifiers pressed first, main keys pressed after).

    A chord made only of shift keys (``EXTEND``) treats the last one as the
    main key so the usual press order still applies.
    """
    modifiers = tuple(k for k in chord if k in MODIFIERS)
    mains = tuple(k for k in chord if k not in MODIFIERS)
    if not mains:
        modifiers, mains = modifiers[:-1], modifiers[-1:]
    return modifiers, mains


def char_to_chord(ch: str) -> Chord | None:
    """Return the chord that types ``ch`` literally, or None if unsupported."""
    if len(ch) != 1:
        return None
    if ch in string.ascii_lowercase:
        return (ch.upper(),)
    if ch in string.ascii_uppercase:
        return (CAPS_SHIFT, ch)
    if ch in string.digits:
        return (ch,)
    if ch == " ":
        return (SPACE,)
    if ch == "\n":
        return (ENTER,)
    if ch in SYMBOL_KEYS:
        return (SYMBOL_SHIFT, SYMBOL_KEYS[ch])
    return None


def text_to_chords(text: str) -> list[Chord]:
    """Translate a literal string into the chords that type it.

    Raises ValueError naming the first unsupported character and its position,
    before anything is typed.
    """
    chords: list[Chord] = []
    for pos, ch in enumerate(text):
        chord = char_to_chord(ch)
        if chord is None:
            raise ValueError(f"unsupported character {ch!r} at position {pos}")
        chords.append(chord)
    return chords
