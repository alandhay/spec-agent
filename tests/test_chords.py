"""Chord ordering, timing and key-release guarantees, using a fake input driver."""

import pytest

from specbot.emulator import FRAME_S
from specbot.keys import CAPS_SHIFT, SYMBOL_SHIFT
from specbot.window_backend import ChordPlayer, default_keymap


class FakeDriver:
    def __init__(self, fail_on_down=None):
        self.events = []
        self.fail_on_down = fail_on_down

    def key_down(self, key):
        self.events.append(("down", key))
        if key == self.fail_on_down:
            raise RuntimeError("boom")

    def key_up(self, key):
        self.events.append(("up", key))

    def held(self):
        down = set()
        for kind, key in self.events:
            if kind == "down":
                down.add(key)
            elif kind == "up":
                down.discard(key)
        return down


def make_player(driver, sleep_log=None, sleep=None):
    def record(seconds):
        driver.events.append(("sleep", round(seconds / FRAME_S)))

    return ChordPlayer(driver, default_keymap(), sleep or record)


def test_default_keymap_covers_all_keys():
    keymap = default_keymap()
    assert len(keymap) == 40
    assert keymap[CAPS_SHIFT] == "shift"
    assert keymap[SYMBOL_SHIFT] == "ctrl"
    assert keymap["A"] == "a" and keymap["ENTER"] == "enter" and keymap["SPACE"] == "space"


def test_modifier_down_first_and_up_last():
    driver = FakeDriver()
    make_player(driver).play((SYMBOL_SHIFT, "P"), hold_frames=3)
    assert driver.events == [
        ("down", "ctrl"),
        ("sleep", 1),
        ("down", "p"),
        ("sleep", 3),
        ("up", "p"),
        ("up", "ctrl"),
    ]


def test_modifier_order_is_independent_of_argument_order():
    driver = FakeDriver()
    make_player(driver).play(("P", SYMBOL_SHIFT), hold_frames=2)
    assert [e for e in driver.events if e[0] != "sleep"] == [
        ("down", "ctrl"),
        ("down", "p"),
        ("up", "p"),
        ("up", "ctrl"),
    ]


def test_plain_key_has_no_modifier_delay():
    driver = FakeDriver()
    make_player(driver).play(("A",), hold_frames=3)
    assert driver.events == [("down", "a"), ("sleep", 3), ("up", "a")]


def test_extend_presses_both_shifts():
    driver = FakeDriver()
    make_player(driver).play((CAPS_SHIFT, SYMBOL_SHIFT), hold_frames=3)
    assert [e for e in driver.events if e[0] != "sleep"] == [
        ("down", "shift"),
        ("down", "ctrl"),
        ("up", "ctrl"),
        ("up", "shift"),
    ]


def test_keys_released_when_key_down_fails():
    driver = FakeDriver(fail_on_down="p")
    with pytest.raises(RuntimeError):
        make_player(driver).play((SYMBOL_SHIFT, "P"), hold_frames=3)
    assert driver.held() == set()
    assert driver.events[-2:] == [("up", "p"), ("up", "ctrl")]


def test_keys_released_on_keyboard_interrupt_during_hold():
    driver = FakeDriver()
    sleeps = []

    def interrupted_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:  # the hold, after the modifier delay
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        make_player(driver, sleep=interrupted_sleep).play((CAPS_SHIFT, "A"), hold_frames=3)
    assert driver.held() == set()
    assert driver.events == [("down", "shift"), ("down", "a"), ("up", "a"), ("up", "shift")]


def test_release_all_releases_every_mapped_host_key():
    driver = FakeDriver()
    player = make_player(driver)
    player.release_all()
    released = [key for kind, key in driver.events if kind == "up"]
    assert sorted(released) == sorted(set(default_keymap().values()))


def test_custom_keymap_is_used():
    driver = FakeDriver()
    keymap = {**default_keymap(), SYMBOL_SHIFT: "altright"}
    ChordPlayer(driver, keymap, lambda s: None).play((SYMBOL_SHIFT, "P"), hold_frames=1)
    assert driver.events[0] == ("down", "altright")
