import pytest
from PIL import Image, ImageDraw

from specbot.capture import (
    apply_crop,
    changed_fraction,
    detect_display,
    detect_scale,
    is_blank,
    postprocess,
    scale_to_native,
)
from specbot.errors import CaptureError

WHITE = (215, 215, 215)
BLACK = (0, 0, 0)
GREY = (240, 240, 240)


def spectrum_screen(scale=1, border=WHITE):
    """A 320x240 'display': border, with some 1-pixel detail in the paper area."""
    img = Image.new("RGB", (320, 240), border)
    draw = ImageDraw.Draw(img)
    for x in range(32, 288, 2):
        draw.point((x, 100), BLACK)
    for y in range(24, 216, 3):
        draw.point((40, y), (0, 0, 215))
    draw.rectangle((100, 50, 180, 90), fill=(215, 0, 0))
    if scale != 1:
        img = img.resize((320 * scale, 240 * scale), Image.NEAREST)
    return img


def with_chrome(display, left=0, top=0, right=0, bottom=0):
    """Surround a display with busy 'emulator chrome' strips."""
    w, h = display.width + left + right, display.height + top + bottom
    img = Image.new("RGB", (w, h), GREY)
    draw = ImageDraw.Draw(img)
    for x in range(0, w, 7):  # icons / text in the strips
        draw.line((x, 0, x, h), fill=(90, 90, 90))
    for y in range(0, h, 5):
        draw.line((0, y, w, y), fill=(120, 120, 120))
    img.paste(display, (left, top))
    return img


def test_apply_crop():
    img = Image.new("RGB", (100, 80))
    assert apply_crop(img, (0, 0, 0, 0)) is img
    assert apply_crop(img, None) is img
    assert apply_crop(img, (10, 5, 20, 15)).size == (70, 60)


def test_apply_crop_keeps_the_right_pixels():
    img = Image.new("RGB", (10, 10), BLACK)
    img.putpixel((3, 2), WHITE)
    cropped = apply_crop(img, (3, 2, 0, 0))
    assert cropped.getpixel((0, 0)) == WHITE


def test_apply_crop_rejects_crop_larger_than_image():
    with pytest.raises(CaptureError, match="leaves nothing"):
        apply_crop(Image.new("RGB", (100, 80)), (60, 0, 40, 0))


def test_scale_to_native_is_exact_for_integer_scales():
    native = spectrum_screen()
    for scale in (2, 3):
        assert scale_to_native(spectrum_screen(scale), (320, 240)).tobytes() == native.tobytes()
    assert scale_to_native(native, None) is native
    assert scale_to_native(native, (320, 240)) is native


def test_postprocess_crops_then_scales():
    native = spectrum_screen()
    raw = with_chrome(spectrum_screen(3), bottom=24)
    out = postprocess(raw, crop=(0, 0, 0, 24), native_size=(320, 240))
    assert out.size == (320, 240)
    assert out.tobytes() == native.tobytes()


def test_is_blank():
    assert is_blank(Image.new("RGB", (8, 8)))
    assert not is_blank(spectrum_screen())


def test_changed_fraction():
    a = Image.new("RGB", (10, 10), BLACK)
    assert changed_fraction(a, a.copy()) == 0.0
    b = a.copy()
    for x in range(5):
        b.putpixel((x, 0), (1, 0, 0))  # even a tiny channel difference counts
    assert changed_fraction(a, b) == pytest.approx(0.05)
    assert changed_fraction(a, Image.new("RGB", (10, 10), WHITE)) == 1.0
    assert changed_fraction(a, Image.new("RGB", (5, 5))) == 1.0


def test_detect_display_without_chrome():
    img = spectrum_screen(2)
    assert detect_display(img) == (0, 0, 640, 480)


@pytest.mark.parametrize(
    "chrome",
    [
        dict(bottom=24),
        dict(top=30, bottom=24),
        dict(left=12, top=30, right=8, bottom=24),
        dict(left=40),
    ],
)
def test_detect_display_trims_chrome(chrome):
    display = spectrum_screen(3)
    img = with_chrome(display, **chrome)
    left, top = chrome.get("left", 0), chrome.get("top", 0)
    assert detect_display(img) == (left, top, left + 960, top + 720)


def test_detect_display_with_plain_status_bar():
    # A flat grey status bar: its rows are uniform, but not the border colour.
    display = spectrum_screen(2)
    img = Image.new("RGB", (640, 480 + 20), GREY)
    img.paste(display, (0, 0))
    assert detect_display(img) == (0, 0, 640, 480)


def test_detect_display_gives_up_on_noise():
    img = Image.effect_noise((400, 300), 80).convert("RGB")
    assert detect_display(img) is None


def test_detect_scale():
    assert detect_scale(spectrum_screen(1)) == 1
    assert detect_scale(spectrum_screen(2)) == 2
    assert detect_scale(spectrum_screen(3)) == 3


def test_detect_scale_on_blank_display_assumes_spectrum_size():
    assert detect_scale(Image.new("RGB", (960, 720), WHITE)) == 3
