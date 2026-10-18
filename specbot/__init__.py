"""specbot - screenshot & keypress automation for ZX Spectrum emulators on Windows."""

from .config import Config, load_config
from .emulator import Emulator, open_emulator
from .errors import (
    CaptureError,
    ConfigError,
    FocusError,
    SpecbotError,
    WindowNotFoundError,
    ZrcpError,
)

__version__ = "0.1.0"

__all__ = [
    "CaptureError",
    "Config",
    "ConfigError",
    "Emulator",
    "FocusError",
    "SpecbotError",
    "WindowNotFoundError",
    "ZrcpError",
    "load_config",
    "open_emulator",
]
