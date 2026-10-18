"""specart: any image format, size or depth must convert; the encoder must round-trip."""

import io

import numpy as np
import pytest
from PIL import Image

pytest.importorskip("numba")
from specart import convert, multicolour, pipeline  # noqa: E402


def photo(size=(120, 90)):
    """A colourful synthetic picture."""
    w, h = size
    y, x = np.mgrid[0:h, 0:w]
    rgb = np.stack([255 * x / max(w - 1, 1), 255 * y / max(h - 1, 1), 128 + 127 * np.sin(x / 7.0)], axis=-1)
    return Image.fromarray(rgb.astype(np.uint8), "RGB")


def as_bytes(img, fmt, **options):
    buf = io.BytesIO()
    img.save(buf, fmt, **options)
    return buf.getvalue()


def cases():
    base = photo()
    rgba = base.convert("RGBA")
    rgba.putalpha(Image.linear_gradient("L").resize(base.size))
    grey16 = Image.fromarray((np.linspace(0, 65535, 120 * 90).reshape(90, 120)).astype(np.uint16))
    frames = [photo(), photo().rotate(90, expand=False)]
    yield "jpeg", as_bytes(base, "JPEG")
    yield "jpeg cmyk", as_bytes(base.convert("CMYK"), "JPEG")
    yield "png rgba", as_bytes(rgba, "PNG")
    yield "png palette + transparency", as_bytes(base.convert("P", palette=Image.ADAPTIVE, colors=16), "PNG", transparency=0)
    yield "gif animated", as_bytes(frames[0], "GIF", save_all=True, append_images=frames[1:])
    yield "bmp 1-bit", as_bytes(base.convert("1"), "BMP")
    yield "tiff 16-bit grey", as_bytes(grey16, "TIFF")
    yield "webp", as_bytes(base, "WEBP")
    yield "greyscale png", as_bytes(base.convert("L"), "PNG")
    yield "tiny 3x2", as_bytes(photo((3, 2)), "PNG")
    yield "one pixel", as_bytes(photo((1, 1)), "PNG")
    yield "huge 6000x4000", as_bytes(photo((6000, 4000)), "JPEG", quality=60)
    yield "tall 50x900", as_bytes(photo((50, 900)), "PNG")
    yield "wide 900x40", as_bytes(photo((900, 40)), "PNG")


@pytest.mark.parametrize("name,data", list(cases()), ids=[n for n, _ in cases()])
def test_any_image_opens_and_fits(name, data):
    img = convert.open_image(data)
    assert img.mode == "RGB"
    for fit in ("fit", "fill", "stretch"):
        target = convert.fit_image(img, fit)
        assert target.shape == (192, 256, 3)
        assert np.isfinite(target).all() and target.min() >= 0 and target.max() <= 1


def test_fit_keeps_shape_and_fill_covers():
    wide = convert.fit_image(Image.new("RGB", (400, 100), (255, 255, 255)), "fit")
    assert wide[96, 128].min() > 0.99 and wide[0, 128].max() == 0          # bars above and below
    assert convert.fit_image(Image.new("RGB", (400, 100), (255, 255, 255)), "fill").min() > 0.99


def test_transparency_becomes_black():
    clear = Image.new("RGBA", (10, 10), (255, 0, 0, 0))
    assert convert.fit_image(convert.open_image(as_bytes(clear, "PNG"))).max() == 0


def test_not_an_image_is_an_error():
    with pytest.raises(Exception):
        convert.open_image(b"this is not an image")


@pytest.mark.parametrize("multi", [True, False])
def test_convert_respects_the_two_colours_per_cell_rule_and_round_trips(multi):
    target = convert.fit_image(photo((256, 192)))
    heights = convert.cell_heights(multi)
    indices, pairs = convert.dither(target, heights)
    before = convert.error_of(target, indices)
    indices, pairs = convert.refine(target, indices, pairs, heights, max_passes=2)
    assert convert.error_of(target, indices) < before                       # refinement helps

    for c in range(32):
        h = int(heights[c])
        for y in range(0, 192, h):
            cell = indices[y : y + h, c * 8 : c * 8 + 8]
            assert set(np.unique(cell)) <= set(pairs[y // 2, c]), "a cell uses a colour outside its pair"
            assert (pairs[y // 2 : y // 2 + h // 2, c] == pairs[y // 2, c]).all()

    # Decode the Spectrum data again and compare with the intended picture.
    bitmap, attrs = convert.encode(indices, pairs)
    assert len(bitmap) == 6144 and attrs.shape == (96, 32)
    decoded = np.zeros((192, 256, 3), dtype=np.uint8)
    for y in range(192):
        addr = (y & 0xC0) << 5 | (y & 0x07) << 8 | (y & 0x38) << 2
        bits = np.unpackbits(np.frombuffer(bitmap[addr : addr + 32], np.uint8))
        for c in range(32):
            a = int(attrs[y // 2, c])
            bright = 8 if a & 0x40 else 0
            ink, paper = convert.PALETTE_SRGB[bright + (a & 7)], convert.PALETTE_SRGB[bright + (a >> 3 & 7)]
            decoded[y, c * 8 : c * 8 + 8] = np.where(bits[c * 8 : c * 8 + 8, None], ink, paper)
    assert (decoded == convert.render(indices)).all()
    if not multi:
        assert (attrs.reshape(24, 4, 32) == attrs[::4, None, :]).all()       # one attribute per 8x8 cell


def test_multicolour_timing_fits_for_any_attributes():
    rng = np.random.default_rng(1)
    code, info = multicolour.generate(rng.integers(0, 128, (96, 32)).astype(np.uint8))
    assert info["slack_after_beam"] >= 8 and info["slack_before_beam"] >= 8
    assert info["end_mod_4"] == 0
    assert multicolour.ORG + len(code) < multicolour.STACK


def test_pipeline_without_emulator(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "JOBS_DIR", tmp_path)
    job = pipeline.start_job(as_bytes(photo(), "PNG"), "test.png", effort="quick", use_emulator=False, wait=True)
    status = job.snapshot()
    assert status["state"] == "done", status["error"]
    assert [s["state"] for s in status["stages"]] == ["done", "done", "done", "done", "skipped", "done"]
    assert (job.dir / "picture.tap").stat().st_size > 6912
    assert len((job.dir / "picture.scr").read_bytes()) == 6912
    assert status["scores"][-1]["value"] <= status["scores"][0]["value"]
    with pytest.raises(ValueError):
        pipeline.start_job(b"", "x", mode="hires")
