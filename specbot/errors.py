"""Exception types raised by specbot."""


class SpecbotError(Exception):
    """Base class for every error specbot raises deliberately."""


class ConfigError(SpecbotError):
    """The configuration file or an override is invalid."""


class WindowNotFoundError(SpecbotError):
    """No emulator window (or more than one) matched the configuration."""


class CaptureError(SpecbotError):
    """The emulator display could not be captured."""


class FocusError(SpecbotError):
    """The emulator window could not be brought to the foreground."""


class ZrcpError(SpecbotError):
    """The ZEsarUX remote protocol connection failed or returned an error."""
