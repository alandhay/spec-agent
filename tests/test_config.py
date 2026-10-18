import pytest

from specbot.config import load_config
from specbot.errors import ConfigError

EXAMPLE = """
backend = "window"

[window]
window_title = "Fuse"
capture = "mss"
crop = [0, 0, 0, 24]
native_size = [320, 240]
hold_frames = 4
gap_frames = 5

[window.keymap]
caps_shift = "shift"
SYMBOL_SHIFT = "altright"

[zrcp]
host = "127.0.0.1"
port = 10001

[launch]
path = "C:/Emulators/fuse/fuse.exe"
args = ["--machine", "128"]
"""


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Run in an empty directory so a stray specbot.toml is never picked up."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SPECBOT_CONFIG", raising=False)


def write(tmp_path, text, name="specbot.toml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_without_a_file():
    cfg = load_config()
    assert cfg.backend == "window"
    assert cfg.window.window_title is None
    assert cfg.window.capture == "mss"
    assert cfg.window.crop == (0, 0, 0, 0)
    assert cfg.window.native_size is None
    assert (cfg.window.hold_frames, cfg.window.gap_frames) == (3, 3)
    assert (cfg.zrcp.host, cfg.zrcp.port) == ("127.0.0.1", 10000)
    assert cfg.launch is None


def test_loads_every_section(tmp_path):
    cfg = load_config(write(tmp_path, EXAMPLE, "other.toml"))
    assert cfg.window.window_title == "Fuse"
    assert cfg.window.crop == (0, 0, 0, 24)
    assert cfg.window.native_size == (320, 240)
    assert (cfg.window.hold_frames, cfg.window.gap_frames) == (4, 5)
    assert cfg.window.keymap == {"CAPS_SHIFT": "shift", "SYMBOL_SHIFT": "altright"}
    assert cfg.zrcp.port == 10001
    assert cfg.launch.path == "C:/Emulators/fuse/fuse.exe"
    assert cfg.launch.args == ["--machine", "128"]


def test_default_file_in_cwd_and_env_var(tmp_path, monkeypatch):
    write(tmp_path, '[window]\nwindow_title = "FromCwd"\n')
    assert load_config().window.window_title == "FromCwd"
    env = write(tmp_path, '[window]\nwindow_title = "FromEnv"\n', "env.toml")
    monkeypatch.setenv("SPECBOT_CONFIG", str(env))
    assert load_config().window.window_title == "FromEnv"


def test_overrides_beat_the_file(tmp_path):
    cfg = load_config(
        write(tmp_path, EXAMPLE),
        title="ZEsarUX",
        capture="printwindow",
        crop=[1, 2, 3, 4],
        hold_frames=6,
        port=12345,
        keymap={"symbol_shift": "ctrl"},
        gap_frames=None,  # None means "not given"
    )
    assert cfg.window.window_title == "ZEsarUX"
    assert cfg.window.capture == "printwindow"
    assert cfg.window.crop == (1, 2, 3, 4)
    assert cfg.window.hold_frames == 6
    assert cfg.window.gap_frames == 5
    assert cfg.zrcp.port == 12345
    assert cfg.window.keymap == {"CAPS_SHIFT": "shift", "SYMBOL_SHIFT": "ctrl"}


def test_shared_fields_follow_the_active_backend(tmp_path):
    cfg = load_config(write(tmp_path, EXAMPLE), backend="zrcp", crop=[9, 9, 9, 9], hold_frames=7)
    assert cfg.backend == "zrcp"
    assert cfg.active is cfg.zrcp
    assert cfg.zrcp.crop == (9, 9, 9, 9) and cfg.zrcp.hold_frames == 7
    assert cfg.window.crop == (0, 0, 0, 24) and cfg.window.hold_frames == 4


def test_launch_overrides():
    cfg = load_config(launch_path="fuse.exe", launch_args=["--no-sound"])
    assert (cfg.launch.path, cfg.launch.args) == ("fuse.exe", ["--no-sound"])
    cfg = load_config(launch={"path": "z.exe", "args": ["--x"]})
    assert (cfg.launch.path, cfg.launch.args) == ("z.exe", ["--x"])


@pytest.mark.parametrize(
    "toml,message",
    [
        ('backend = "telnet"', "backend must be one of"),
        ('[window]\ncapture = "gdi"', "capture must be one of"),
        ("[window]\ncrop = [1, 2, 3]", "crop must be four"),
        ("[window]\ncrop = [0, 0, -1, 0]", "crop must be four"),
        ("[window]\nnative_size = [320]", "native_size must be two"),
        ("[window]\nnative_size = [0, 240]", "native_size must be two"),
        ("[window]\nhold_frames = 0", "hold_frames"),
        ('[window]\ngap_frames = "3"', "gap_frames"),
        ("[window]\nwindow_title = 5", "window_title"),
        ('[window]\nwindow_titel = "Fuse"', "unknown key(s) in [window]: window_titel"),
        ('[window.keymap]\nF1 = "f1"', "unknown Spectrum key"),
        ("[window.keymap]\nA = 5", "host key name"),
        ("[zrcp]\nport = 70000", "port must be"),
        ('[zrcp.keycodes]\nENTER = "x"', "non-negative integer"),
        ("[launch]\nargs = []", "[launch] needs a path"),
        ('[launch]\npath = "x"\nargs = "--y"', "args must be a list"),
        ("[screen]\nx = 1", "unknown top-level key(s): screen"),
        ("backend = ", "invalid TOML"),
    ],
)
def test_validation_errors(tmp_path, toml, message):
    with pytest.raises(ConfigError) as exc:
        load_config(write(tmp_path, toml))
    assert message in str(exc.value)


def test_missing_explicit_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")


def test_bad_overrides():
    with pytest.raises(ConfigError, match="unknown option 'colour'"):
        load_config(colour="red")
    with pytest.raises(ConfigError, match="backend must be one of"):
        load_config(backend="nope")
    with pytest.raises(ConfigError, match="hold_frames"):
        load_config(hold_frames=0)
    with pytest.raises(ConfigError, match="launch_args"):
        load_config(launch_args=["--x"])


def test_example_file_is_valid():
    from pathlib import Path

    example = Path(__file__).resolve().parent.parent / "specbot.toml.example"
    cfg = load_config(example)
    assert cfg.backend == "window"
    assert cfg.window.window_title == "Fuse"
