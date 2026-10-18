import pytest

from specbot.keys import (
    ALIASES,
    CAPS_SHIFT,
    ENTER,
    SPACE,
    SPECTRUM_KEYS,
    SYMBOL_SHIFT,
    char_to_chord,
    normalize_key,
    resolve,
    resolve_chord,
    split_chord,
    text_to_chords,
)

# The table from section 4 of the spec.
SYMBOL_TABLE = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "&": "6",
    "'": "7", "(": "8", ")": "9", "_": "0", "<": "R", ">": "T",
    ";": "O", '"': "P", "^": "H", "-": "J", "+": "K", "=": "L",
    ":": "Z", "£": "X", "?": "C", "/": "V", "*": "B", ",": "N",
    ".": "M",
}


def test_there_are_forty_keys():
    assert len(SPECTRUM_KEYS) == 40


@pytest.mark.parametrize("char,key", SYMBOL_TABLE.items())
def test_symbols_use_symbol_shift(char, key):
    assert char_to_chord(char) == (SYMBOL_SHIFT, key)


def test_lowercase_letters_are_plain_keys():
    for ch in "abcdefghijklmnopqrstuvwxyz":
        assert char_to_chord(ch) == (ch.upper(),)


def test_uppercase_letters_use_caps_shift():
    for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        assert char_to_chord(ch) == (CAPS_SHIFT, ch)


def test_digits_space_newline():
    for ch in "0123456789":
        assert char_to_chord(ch) == (ch,)
    assert char_to_chord(" ") == (SPACE,)
    assert char_to_chord("\n") == (ENTER,)


def test_text_to_chords():
    assert text_to_chords('Hi "x"\n') == [
        (CAPS_SHIFT, "H"),
        ("I",),
        (SPACE,),
        (SYMBOL_SHIFT, "P"),
        ("X",),
        (SYMBOL_SHIFT, "P"),
        (ENTER,),
    ]


def test_unsupported_character_names_char_and_position():
    with pytest.raises(ValueError) as exc:
        text_to_chords("ab~c")
    assert "'~'" in str(exc.value)
    assert "position 2" in str(exc.value)


@pytest.mark.parametrize(
    "alias,chord",
    [
        ("DELETE", (CAPS_SHIFT, "0")),
        ("BACKSPACE", (CAPS_SHIFT, "0")),
        ("LEFT", (CAPS_SHIFT, "5")),
        ("DOWN", (CAPS_SHIFT, "6")),
        ("UP", (CAPS_SHIFT, "7")),
        ("RIGHT", (CAPS_SHIFT, "8")),
        ("EDIT", (CAPS_SHIFT, "1")),
        ("BREAK", (CAPS_SHIFT, SPACE)),
        ("EXTEND", (CAPS_SHIFT, SYMBOL_SHIFT)),
    ],
)
def test_aliases(alias, chord):
    assert ALIASES[alias] == chord
    assert resolve(alias) == chord
    assert resolve(alias.lower()) == chord


def test_key_names_are_case_insensitive():
    assert normalize_key("enter") == ENTER
    assert normalize_key("symbol_shift") == SYMBOL_SHIFT
    assert normalize_key("a") == "A"
    assert resolve("Caps Shift") == (CAPS_SHIFT,)


def test_unknown_key_raises():
    with pytest.raises(ValueError, match="F1"):
        resolve("F1")
    with pytest.raises(ValueError):
        normalize_key("DELETE")  # an alias is not a single physical key


def test_resolve_chord_forms():
    assert resolve_chord("SYMBOL_SHIFT+P") == (SYMBOL_SHIFT, "P")
    assert resolve_chord(("symbol_shift", "p")) == (SYMBOL_SHIFT, "P")
    assert resolve_chord(("CAPS_SHIFT", "DELETE")) == (CAPS_SHIFT, "0")  # de-duplicated
    with pytest.raises(ValueError):
        resolve_chord(())
    with pytest.raises(ValueError):
        resolve_chord("A+")


def test_split_chord():
    assert split_chord(("A",)) == ((), ("A",))
    assert split_chord(("P", SYMBOL_SHIFT)) == ((SYMBOL_SHIFT,), ("P",))
    assert split_chord((CAPS_SHIFT, SYMBOL_SHIFT)) == ((CAPS_SHIFT,), (SYMBOL_SHIFT,))
