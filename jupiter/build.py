"""Build the Jupiter picture for the ZX Spectrum.

    python build.py            convert (cached) and write the tapes
    python build.py --fresh    redo the slow image conversion
    python build.py --verify   also load each tape in the emulator and compare the
                               screen with the intended picture, pixel by pixel

Outputs (in this folder):
    jupiter.tap          128K multicolour version (8x2 colour cells)
    jupiter48.tap        plain screen for any Spectrum (8x8 colour cells)
    jupiter48.scr        the same as a raw 6912-byte screen file
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import convert  # noqa: E402
from specart import multicolour  # noqa: E402
from specbot.tape import code_tap, program_tap, tokenize_program  # noqa: E402

SOURCE = HERE / "source" / "hubble2019.jpg"
BUILD = HERE / "build"


def converted(name: str, cell_h: int, fresh: bool):
    cache = BUILD / f"{name}.npz"
    if cache.exists() and not fresh:
        data = np.load(cache)
        return data["target"], data["indices"], data["bitmap"].tobytes(), data["attrs"]
    print(f"converting ({name}; this takes a few minutes)")
    target, indices, bitmap, attrs = convert.convert(SOURCE, cell_h)
    np.savez_compressed(cache, target=target, indices=indices, bitmap=np.frombuffer(bitmap, np.uint8), attrs=attrs)
    convert.save_previews(indices, name)
    return target, indices, bitmap, attrs


def loader(lines: list[str]) -> bytes:
    return program_tap(tokenize_program("\n".join(lines)), "JUPITER", autostart=10)


def build(fresh: bool) -> dict:
    BUILD.mkdir(exist_ok=True)
    out = {}

    # Standard screen: works on every Spectrum.
    target, indices, bitmap, attrs = converted("standard", 8, fresh)
    screen = bitmap + attrs.tobytes()
    (HERE / "jupiter48.scr").write_bytes(screen)
    tap = loader(['10 BORDER 0: PAPER 0: INK 0: CLS', '20 LOAD "" SCREEN$', "30 PAUSE 0"])
    (HERE / "jupiter48.tap").write_bytes(tap + code_tap(screen, 16384, "jupiter"))
    print(f"jupiter48: perceived error {convert.perceived_error(target, indices):.4f}")
    out["jupiter48"] = indices

    # Multicolour: 8x2 cells in the middle 24 columns, 128K timing.
    target, indices, bitmap, attrs = converted("multicolour", 2, fresh)
    code, info = multicolour.generate(attrs)
    screen = bitmap + attrs[::4].tobytes()              # attribute file = each row's first pair
    # The screen loads last: LOAD prints each block's name, which would spoil a picture already there.
    tap = loader(['10 CLEAR 32767: BORDER 0: PAPER 0: INK 0: CLS', '20 LOAD "" CODE: LOAD "" CODE',
                  f"30 RANDOMIZE USR {multicolour.ORG}"])
    (HERE / "jupiter.tap").write_bytes(tap + code_tap(code, multicolour.ORG, "jupiter") + code_tap(screen, 16384, "screen"))
    print(f"jupiter: perceived error {convert.perceived_error(target, indices):.4f}; display routine {info}")
    out["jupiter"] = indices
    return out


def verify(name: str, indices: np.ndarray) -> bool:
    import specbot

    with specbot.open_emulator(str(ROOT / "specbot.toml")) as emu:
        emu.load_file(str(HERE / f"{name}.tap"))
        emu.wait_frames(500)
        shots = [np.asarray(emu.screenshot().convert("RGB")) for _ in range(3)]
    want = convert.render(indices)
    ok = True
    for n, shot in enumerate(shots):
        top, left = (shot.shape[0] - 192) // 2, (shot.shape[1] - 256) // 2
        got = shot[top : top + 192, left : left + 256]
        wrong = (got != want).any(axis=-1)
        if n == 0:
            Image.fromarray(shot).resize((shot.shape[1] * 3, shot.shape[0] * 3), Image.NEAREST).save(BUILD / f"{name}_emulator.png")
        if wrong.any():
            ok = False
            ys, xs = np.nonzero(wrong)
            print(f"{name}: capture {n}: {wrong.sum()} pixels differ; lines {ys.min()}-{ys.max()}, "
                  f"columns {sorted(set(xs // 8))}")
    border = np.concatenate([shots[0][:20].reshape(-1, 3), shots[0][:, :28].reshape(-1, 3)])
    if border.any():
        ok = False
        print(f"{name}: the border is not black")
    print(f"{name}: {'emulator screen matches the intended picture exactly' if ok else 'MISMATCH'}")
    return ok


if __name__ == "__main__":
    pictures = build("--fresh" in sys.argv)
    if "--verify" in sys.argv:
        results = [verify(name, indices) for name, indices in pictures.items()]
        sys.exit(0 if all(results) else 1)
