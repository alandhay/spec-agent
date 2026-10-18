"""Load and validate specbot configuration (TOML file + keyword overrides)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .keys import normalize_key

BACKENDS = ("window", "zrcp")
CAPTURE_MODES = ("mss", "printwindow")
DEFAULT_CONFIG_NAME = "specbot.toml"
CONFIG_ENV_VAR = "SPECBOT_CONFIG"


@dataclass
class WindowConfig:
    window_title: str | None = None
    window_class: str | None = None
    capture: str = "mss"
    crop: tuple[int, int, int, int] = (0, 0, 0, 0)
    native_size: tuple[int, int] | None = None
    hold_frames: int = 3
    gap_frames: int = 3
    # Spectrum key -> host key name (pydirectinput); merged over the defaults.
    keymap: dict[str, str] = field(default_factory=dict)


@dataclass
class ZrcpConfig:
    host: str = "127.0.0.1"
    port: int = 10000
    crop: tuple[int, int, int, int] = (0, 0, 0, 0)
    native_size: tuple[int, int] | None = None
    hold_frames: int = 3
    gap_frames: int = 3
    # Spectrum key -> ZEsarUX util_teclas code; merged over the defaults.
    keycodes: dict[str, int] = field(default_factory=dict)


@dataclass
class LaunchConfig:
    path: str
    args: list[str] = field(default_factory=list)


@dataclass
class Config:
    backend: str = "window"
    window: WindowConfig = field(default_factory=WindowConfig)
    zrcp: ZrcpConfig = field(default_factory=ZrcpConfig)
    launch: LaunchConfig | None = None
    # Spectrum ROM image holding the character set, used to read screen text.
    rom: str | None = None

    @property
    def rom_path(self) -> str | None:
        """The configured ROM, else roms/48.rom next to the launched emulator."""
        if self.rom:
            return self.rom
        if self.launch:
            guess = Path(self.launch.path).parent / "roms" / "48.rom"
            if guess.is_file():
                return str(guess)
        return None

    @property
    def active(self) -> WindowConfig | ZrcpConfig:
        """The section for the selected backend."""
        return self.window if self.backend == "window" else self.zrcp


_OVERRIDE_ALIASES = {"title": "window_title"}
_KEY_TABLES = ("keymap", "keycodes")


def _field_names(cls) -> set[str]:
    return {f.name for f in fields(cls)}


def _fill(section, data: dict[str, Any], where: str) -> None:
    if not isinstance(data, dict):
        raise ConfigError(f"[{where}] must be a table")
    unknown = set(data) - _field_names(type(section))
    if unknown:
        raise ConfigError(f"unknown key(s) in [{where}]: {', '.join(sorted(unknown))}")
    for name, value in data.items():
        setattr(section, name, value)


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from None


def find_config_path(config_path: str | os.PathLike | None) -> Path | None:
    """Explicit path, else $SPECBOT_CONFIG, else ./specbot.toml if it exists."""
    if config_path:
        return Path(config_path)
    env = os.environ.get(CONFIG_ENV_VAR)
    if env:
        return Path(env)
    default = Path(DEFAULT_CONFIG_NAME)
    return default if default.is_file() else None


def _apply_overrides(cfg: Config, overrides: dict[str, Any]) -> None:
    overrides = {
        _OVERRIDE_ALIASES.get(k, k): v for k, v in overrides.items() if v is not None
    }
    if "backend" in overrides:
        cfg.backend = overrides.pop("backend")
    if cfg.backend not in BACKENDS:
        return  # reported by _validate
    if "rom" in overrides:
        cfg.rom = overrides.pop("rom")

    launch = overrides.pop("launch", None)
    if isinstance(launch, dict):
        overrides.setdefault("launch_path", launch.get("path"))
        overrides.setdefault("launch_args", launch.get("args"))
    elif launch is not None:
        overrides.setdefault("launch_path", launch)
    launch_path = overrides.pop("launch_path", None)
    launch_args = overrides.pop("launch_args", None)
    if launch_path is not None:
        cfg.launch = LaunchConfig(path=launch_path, args=list(cfg.launch.args) if cfg.launch else [])
    if launch_args is not None:
        if cfg.launch is None:
            raise ConfigError("launch_args given without a launch path")
        cfg.launch.args = launch_args

    # Fields that exist in both sections (crop, hold_frames, ...) go to the
    # active backend; the rest go to whichever section owns them.
    active = cfg.active
    other = cfg.zrcp if active is cfg.window else cfg.window
    for name, value in overrides.items():
        if name in _field_names(type(active)):
            section = active
        elif name in _field_names(type(other)):
            section = other
        else:
            raise ConfigError(f"unknown option {name!r}")
        if name in _KEY_TABLES:
            if not isinstance(value, dict):
                raise ConfigError(f"{name} override must be a mapping")
            setattr(section, name, {**getattr(section, name), **value})
        else:
            setattr(section, name, value)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_common(section: WindowConfig | ZrcpConfig, where: str) -> None:
    crop = section.crop
    if (
        not isinstance(crop, (list, tuple))
        or len(crop) != 4
        or not all(_is_int(v) and v >= 0 for v in crop)
    ):
        raise ConfigError(f"[{where}] crop must be four non-negative integers [left, top, right, bottom]")
    section.crop = tuple(crop)

    size = section.native_size
    if size is not None:
        if (
            not isinstance(size, (list, tuple))
            or len(size) != 2
            or not all(_is_int(v) and v > 0 for v in size)
        ):
            raise ConfigError(f"[{where}] native_size must be two positive integers [width, height]")
        section.native_size = tuple(size)

    if not _is_int(section.hold_frames) or section.hold_frames < 1:
        raise ConfigError(f"[{where}] hold_frames must be an integer >= 1")
    if not _is_int(section.gap_frames) or section.gap_frames < 0:
        raise ConfigError(f"[{where}] gap_frames must be an integer >= 0")


def _normalize_key_table(table: Any, where: str, value_ok, value_desc: str) -> dict:
    if not isinstance(table, dict):
        raise ConfigError(f"[{where}] must be a table")
    result = {}
    for name, value in table.items():
        try:
            key = normalize_key(name)
        except ValueError as exc:
            raise ConfigError(f"[{where}] {exc}") from None
        if not value_ok(value):
            raise ConfigError(f"[{where}] {name} must be {value_desc}")
        result[key] = value
    return result


def _optional_str(value: Any, what: str) -> None:
    if value is not None and (not isinstance(value, str) or not value):
        raise ConfigError(f"{what} must be a non-empty string")


def _validate(cfg: Config) -> None:
    if cfg.backend not in BACKENDS:
        raise ConfigError(f"backend must be one of {', '.join(BACKENDS)} (got {cfg.backend!r})")

    win = cfg.window
    _optional_str(win.window_title, "[window] window_title")
    _optional_str(win.window_class, "[window] window_class")
    if win.capture not in CAPTURE_MODES:
        raise ConfigError(
            f"[window] capture must be one of {', '.join(CAPTURE_MODES)} (got {win.capture!r})"
        )
    _validate_common(win, "window")
    win.keymap = _normalize_key_table(
        win.keymap, "window.keymap", lambda v: isinstance(v, str) and v, "a host key name"
    )

    zrcp = cfg.zrcp
    _optional_str(zrcp.host, "[zrcp] host")
    if zrcp.host is None:
        raise ConfigError("[zrcp] host must be a non-empty string")
    if not _is_int(zrcp.port) or not 1 <= zrcp.port <= 65535:
        raise ConfigError("[zrcp] port must be an integer between 1 and 65535")
    _validate_common(zrcp, "zrcp")
    zrcp.keycodes = _normalize_key_table(
        zrcp.keycodes, "zrcp.keycodes", lambda v: _is_int(v) and v >= 0, "a non-negative integer"
    )

    _optional_str(cfg.rom, "rom")
    if cfg.launch is not None:
        if not isinstance(cfg.launch.path, str) or not cfg.launch.path:
            raise ConfigError("[launch] path must be a non-empty string")
        args = cfg.launch.args
        if not isinstance(args, (list, tuple)) or not all(isinstance(a, str) for a in args):
            raise ConfigError("[launch] args must be a list of strings")
        cfg.launch.args = list(args)


def load_config(config_path: str | os.PathLike | None = None, **overrides: Any) -> Config:
    """Build a validated Config from a TOML file (optional) plus overrides.

    Overrides use the field names from the TOML sections (``window_title``,
    ``crop``, ``port`` ...), plus ``backend``, ``title`` (alias of
    ``window_title``), ``launch_path`` and ``launch_args``. Overrides whose
    value is None are ignored.
    """
    path = find_config_path(config_path)
    data = _read_toml(path) if path else {}

    unknown = set(data) - {"backend", "rom", "window", "zrcp", "launch"}
    if unknown:
        raise ConfigError(f"unknown top-level key(s): {', '.join(sorted(unknown))}")

    cfg = Config()
    if "backend" in data:
        cfg.backend = data["backend"]
    if "rom" in data:
        cfg.rom = data["rom"]
    if "window" in data:
        _fill(cfg.window, data["window"], "window")
    if "zrcp" in data:
        _fill(cfg.zrcp, data["zrcp"], "zrcp")
    if "launch" in data:
        launch = data["launch"]
        if not isinstance(launch, dict):
            raise ConfigError("[launch] must be a table")
        unknown = set(launch) - {"path", "args"}
        if unknown:
            raise ConfigError(f"unknown key(s) in [launch]: {', '.join(sorted(unknown))}")
        if "path" not in launch:
            raise ConfigError("[launch] needs a path")
        cfg.launch = LaunchConfig(path=launch["path"], args=launch.get("args", []))

    _apply_overrides(cfg, dict(overrides))
    _validate(cfg)
    return cfg
