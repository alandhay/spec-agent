"""One conversion job, start to finish, reporting progress as it goes.

The job is the "agent": it fits the image, makes several attempts at the
picture from different starting points, refines each against the eye model,
keeps the best, builds a Spectrum program, runs it on the emulator, and
measures how well what the emulator shows matches the original.
"""

from __future__ import annotations

import json
import threading
import time
import traceback
import uuid
from pathlib import Path

import numpy as np
from PIL import Image

from . import convert, multicolour
from specbot.tape import code_tap, program_tap, tokenize_program

ROOT = Path(__file__).resolve().parent.parent
JOBS_DIR = Path(__file__).resolve().parent / "jobs"
EMULATOR_LOCK = threading.Lock()        # one emulator, one job at a time

STAGES = [
    ("open", "Read the image"),
    ("fit", "Fit it to the Spectrum screen"),
    ("search", "Search for the best picture"),
    ("build", "Build the Spectrum program"),
    ("emulator", "Run it on the emulator"),
    ("judge", "Measure the match"),
]

# Shadow lift: gamma applied to the brightness before the search (1 = as supplied).
TONES = {"original": 1.0, "lift": 0.8, "lift-more": 0.65}

# Starting points for the search: (name, how hard to avoid grainy colour pairs).
ATTEMPTS = {
    "quick": [("error diffusion", 0.02)],
    "thorough": [("error diffusion", 0.02), ("calm colour pairs", 0.12), ("free colour pairs", 0.0)],
}


class Job:
    def __init__(self, name: str, mode: str, fit: str, effort: str, use_emulator: bool = True,
                 tone: str = "original") -> None:
        self.id = uuid.uuid4().hex[:12]
        self.dir = JOBS_DIR / self.id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.use_emulator = use_emulator
        self.status = {
            "id": self.id, "name": name, "mode": mode, "fit": fit, "effort": effort, "tone": tone,
            "state": "running", "error": None,
            "stages": [{"key": k, "label": label, "state": "waiting", "detail": "", "seconds": None} for k, label in STAGES],
            "log": [], "scores": [], "images": {}, "result": {},
        }
        self._started: dict[str, float] = {}
        self._last_image = 0.0

    # -- reporting -------------------------------------------------------------

    def snapshot(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.status))

    def log(self, text: str) -> None:
        with self.lock:
            self.status["log"].append(text)

    def _stage(self, key: str) -> dict:
        return next(s for s in self.status["stages"] if s["key"] == key)

    def begin(self, key: str, detail: str = "") -> None:
        self._started[key] = time.time()
        with self.lock:
            self._stage(key).update(state="working", detail=detail)

    def detail(self, key: str, detail: str) -> None:
        with self.lock:
            self._stage(key)["detail"] = detail

    def finish(self, key: str, detail: str | None = None, state: str = "done") -> None:
        with self.lock:
            stage = self._stage(key)
            stage["state"] = state
            stage["seconds"] = round(time.time() - self._started.get(key, time.time()), 1)
            if detail is not None:
                stage["detail"] = detail

    def save_image(self, key: str, array: np.ndarray) -> None:
        Image.fromarray(array).save(self.dir / f"{key}.png")
        with self.lock:
            self.status["images"][key] = self.status["images"].get(key, 0) + 1

    def show_picture(self, indices: np.ndarray, force: bool = False) -> None:
        """Update the live picture (at most a few times a second)."""
        now = time.time()
        if not force and now - self._last_image < 0.25:
            return
        self._last_image = now
        self.save_image("render", convert.render(indices))
        self.save_image("blended", convert.blended(indices))

    def score(self, label: str, value: float, attempt: int) -> None:
        with self.lock:
            self.status["scores"].append({"label": label, "value": round(value, 5), "attempt": attempt})

    # -- the work --------------------------------------------------------------

    def run(self, data: bytes) -> None:
        try:
            self._run(data)
            with self.lock:
                self.status["state"] = "done"
        except Exception as exc:                      # report, don't crash the server
            traceback.print_exc()
            with self.lock:
                self.status["state"] = "error"
                self.status["error"] = f"{type(exc).__name__}: {exc}"
                for stage in self.status["stages"]:
                    if stage["state"] == "working":
                        stage["state"] = "failed"

    def _run(self, data: bytes) -> None:
        status = self.status
        mode, fit, effort = status["mode"], status["fit"], status["effort"]
        multi = mode == "multicolour"

        self.begin("open")
        raw = Image.open(__import__("io").BytesIO(data))
        described = f"{raw.format or 'image'}, {raw.width}x{raw.height}, mode {raw.mode}"
        img = convert.open_image(data)
        self.finish("open", described)
        self.log(f"Opened {status['name']}: {described}.")

        self.begin("fit")
        target = convert.fit_image(img, fit)
        self.save_image("target", convert.to_srgb(target).round().astype(np.uint8))
        self.finish("fit", f"{fit}, resized in linear light to 256x192")
        # The Spectrum has no dark colours, so deep shadows can only be black or scattered
        # dots. Lifting them trades tonal accuracy for visible detail; the score is always
        # measured against the image as supplied.
        aim = convert.expose(target, TONES[status["tone"]])
        if aim is not target:
            self.log(f"Shadows lifted ({status['tone']}) before the search; the match is still judged against the original.")

        self.begin("search")
        heights = convert.cell_heights(multi)
        cells = "8x2 colour cells in the middle 24 columns, 8x8 at the sides" if multi else "8x8 colour cells"
        self.log(f"Screen: {cells}. Every cell may use two of the Spectrum's 15 colours.")
        attempts = ATTEMPTS[effort]
        best = None
        for n, (name, noise) in enumerate(attempts, 1):
            label = f"attempt {n} of {len(attempts)}: {name}"
            self.detail("search", f"{label} - first pass")
            convert.NOISE_PENALTY = noise
            indices, pairs = convert.dither(aim, heights, progress=lambda indices, **_: self.show_picture(indices))
            start = convert.error_of(target, indices)
            self.score(f"{name}: dithered", start, n)
            self.show_picture(indices, force=True)

            def progress(indices, refine_pass=0, pass_done=False, score=None, cells_changed=0, pixels_flipped=0, **_):
                self.show_picture(indices, force=pass_done)
                if pass_done:
                    score = convert.error_of(target, indices)
                    self.score(f"{name}: refine {refine_pass}", score, n)
                    self.detail("search", f"{label} - refine pass {refine_pass}, error {score:.4f}")
                    self.log(f"  refine pass {refine_pass}: {cells_changed} cells changed colours, "
                             f"{pixels_flipped} pixels flipped, error {score:.4f}")

            self.log(f"Attempt {n} ({name}): dithered error {start:.4f}; refining.")
            indices, pairs = convert.refine(aim, indices, pairs, heights, progress=progress)
            final = convert.error_of(target, indices)
            if best is None or final < best[0]:
                best = (final, indices, pairs, name)
                self.log(f"Attempt {n} finished at {final:.4f}: best so far.")
            else:
                self.log(f"Attempt {n} finished at {final:.4f}: no better than {best[0]:.4f}, discarded.")
        final, indices, pairs, name = best
        self.show_picture(indices, force=True)
        self.finish("search", f"kept '{name}', perceived error {final:.4f}")

        self.begin("build")
        bitmap, attrs = convert.encode(indices, pairs)
        (self.dir / "picture.scr").write_bytes(bitmap + attrs[::4].tobytes())
        if multi:
            code, info = multicolour.generate(attrs)
            lines = ['10 CLEAR 32767: BORDER 0: PAPER 0: INK 0: CLS', '20 LOAD "" CODE: LOAD "" CODE',
                     f"30 RANDOMIZE USR {multicolour.ORG}"]
            blocks = code_tap(code, multicolour.ORG, "display") + code_tap(bitmap + attrs[::4].tobytes(), 16384, "screen")
            detail = (f"128K tape: screen + {info['bytes']} bytes of beam-synchronised code "
                      f"(timing margins {info['slack_after_beam']} / {info['slack_before_beam']} T-states)")
        else:
            lines = ['10 BORDER 0: PAPER 0: INK 0: CLS', '20 LOAD "" SCREEN$', "30 PAUSE 0"]
            blocks = code_tap(bitmap + attrs[::4].tobytes(), 16384, "screen")
            detail = "tape with a standard 6912-byte screen"
        tap = program_tap(tokenize_program("\n".join(lines)), "PICTURE", autostart=10) + blocks
        (self.dir / "picture.tap").write_bytes(tap)
        self.finish("build", detail)

        result = {"perceived_error": round(final, 5), "kept": name, "files": ["picture.tap", "picture.scr"]}
        if not self.use_emulator:
            self.finish("emulator", "skipped", state="skipped")
            self.finish("judge", f"converter's own picture: perceived error {final:.4f}")
            with self.lock:
                status["result"] = result
            return

        self.begin("emulator", "waiting for the emulator")
        want = convert.render(indices)
        with EMULATOR_LOCK:
            import specbot

            with specbot.open_emulator(str(ROOT / "specbot.toml")) as emu:
                self.detail("emulator", "loading the tape in Fuse")
                emu.load_file(str(self.dir / "picture.tap"))
                deadline = time.time() + 40
                shot, wrong, matches = None, None, 0
                while time.time() < deadline:
                    time.sleep(0.5)
                    shot = np.asarray(emu.screenshot().convert("RGB"))
                    top, left = (shot.shape[0] - 192) // 2, (shot.shape[1] - 256) // 2
                    paper = shot[top : top + 192, left : left + 256]
                    wrong = int((paper != want).any(axis=-1).sum())
                    self.save_image("emulator", shot)
                    self.detail("emulator", f"Fuse is showing the picture; {wrong} pixels differ from the intended one"
                                if wrong < 20000 else "loading...")
                    matches = matches + 1 if wrong == 0 else 0
                    if matches >= 2:
                        break
        self.finish("emulator", "captured from Fuse" + (" (128K, multicolour running)" if multi else ""))

        self.begin("judge")
        emulator_error = convert.perceived_error(target, convert.to_linear(paper))
        exact = wrong == 0
        result.update(emulator_error=round(emulator_error, 5), exact=exact, pixels_wrong=wrong)
        verdict = ("the emulator shows exactly the picture the converter intended" if exact
                   else f"{wrong} pixels on the emulator differ from the intended picture")
        self.log(f"Emulator screen vs original: perceived error {emulator_error:.4f}; {verdict}.")
        self.finish("judge", f"perceived error {emulator_error:.4f} on the emulator; {verdict}",
                    state="done" if exact else "failed")
        with self.lock:
            status["result"] = result


JOBS: dict[str, Job] = {}


def start_job(data: bytes, name: str, mode: str = "multicolour", fit: str = "fit", effort: str = "thorough",
              use_emulator: bool = True, wait: bool = False, tone: str = "original") -> Job:
    if mode not in ("multicolour", "standard"):
        raise ValueError("mode must be multicolour or standard")
    if fit not in ("fit", "fill", "stretch"):
        raise ValueError("fit must be fit, fill or stretch")
    if effort not in ATTEMPTS:
        raise ValueError("effort must be quick or thorough")
    if tone not in TONES:
        raise ValueError("tone must be original, lift or lift-more")
    job = Job(name, mode, fit, effort, use_emulator, tone)
    JOBS[job.id] = job
    if wait:
        job.run(data)
    else:
        threading.Thread(target=job.run, args=(data,), daemon=True).start()
    return job
