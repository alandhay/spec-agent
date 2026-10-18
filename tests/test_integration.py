"""Integration tests: need Windows and a running emulator in 128K BASIC.

Skipped by default. Run with `pytest -m integration` from a directory holding
a specbot.toml for your emulator (or set SPECBOT_CONFIG). The emulator should
be sitting in the 128K BASIC editor.
"""

import pytest

import specbot
from specbot.capture import is_blank

pytestmark = pytest.mark.integration


@pytest.fixture
def emu():
    with specbot.open_emulator() as emulator:
        yield emulator


def test_screenshot_is_not_blank_and_has_expected_size(emu):
    img = emu.screenshot()
    assert not is_blank(img)
    assert len(img.getcolors(maxcolors=1 << 16) or []) != 1, "screenshot is a single flat colour"
    if emu.native_size:
        assert img.size == emu.native_size


def test_typing_changes_the_screen(emu):
    before = emu.screenshot()
    emu.type_text("PRINT 2+2\n")
    assert emu.wait_for_change(timeout_s=5.0, baseline=before)
