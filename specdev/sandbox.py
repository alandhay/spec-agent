"""Keep each person's processes to their own folders, with Linux Landlock.

Hosted, everyone's workbench runs in one service as one Linux user, so the folder
checks in the server are not enough on their own: the assembler will INCBIN any
file it is given a path to, and the agent's process can read whatever the service
can. Every process started for a person (the agent, the assembler, the emulator)
is therefore started through this file, which confines it with Landlock and then
runs it:

    python sandbox.py [--read PATH] [--write PATH] [--no-network] [--set NAME=VALUE] -- COMMAND ...
    python sandbox.py --check          is confinement available and working here?

A confined process can read the system's own files (programs, libraries, /etc),
read and write only the paths it is given, and nothing else: not other people's
folders, not the service's home. With --no-network it cannot open TCP connections
either (kernel 6.7 or later). The SPECDEV_ settings are left out of its environment.
Landlock needs no privileges and cannot be undone by the process it confines.

It is switched on by SPECDEV_SANDBOX=1 (the hosted settings). It is then required:
if the kernel cannot confine, the command is not run. This file uses only the
standard library because it is run as a script, by path, with ``python -I``.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
APP = HERE.parent.parent

# What every confined process may read: the system, and the Python and code it runs on.
SYSTEM = ["/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc", "/run/systemd/resolve", "/proc", "/sys",
          "/dev/urandom", "/dev/random", sys.prefix, sys.base_prefix, str(APP)]
DEVICES = ["/dev/null", "/dev/zero"]        # and may write

# Landlock's system calls (the same numbers on every architecture) and its access rights.
_CREATE, _ADD_RULE, _RESTRICT = 444, 445, 446
_PATH_BENEATH, _VERSION = 1, 1
EXECUTE, WRITE_FILE, READ_FILE, READ_DIR = 1 << 0, 1 << 1, 1 << 2, 1 << 3
REFER, TRUNCATE = 1 << 13, 1 << 14
FILE_RIGHTS = EXECUTE | WRITE_FILE | READ_FILE | TRUNCATE     # all that a rule on a file (not a folder) may name
READ = EXECUTE | READ_FILE | READ_DIR
NET_BIND_TCP, NET_CONNECT_TCP = 1 << 0, 1 << 1
_NO_NEW_PRIVS = 38


def enabled() -> bool:
    return sys.platform.startswith("linux") and os.environ.get("SPECDEV_SANDBOX", "") == "1"


def command(cmd: list[str], *, read: list = (), write: list = (), network: bool = True,
            env: dict[str, str] | None = None) -> list[str]:
    """``cmd`` as it should be started: through this file when confinement is on, else unchanged."""
    if not enabled():
        return list(cmd)
    out = [sys.executable, "-I", str(HERE)]
    for path in read:
        out += ["--read", str(path)]
    for path in write:
        out += ["--write", str(path)]
    if not network:
        out.append("--no-network")
    for name, value in (env or {}).items():
        out += ["--set", f"{name}={value}"]
    return [*out, "--", *cmd]


# -- the confining itself (Linux) ----------------------------------------------

class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64), ("handled_access_net", ctypes.c_uint64)]


class _PathBeneath(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def _libc():
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    return libc


def abi() -> int:
    """The Landlock version this kernel offers; 0 if none."""
    if not sys.platform.startswith("linux"):
        return 0
    version = _libc().syscall(ctypes.c_long(_CREATE), ctypes.c_void_p(None), ctypes.c_size_t(0),
                              ctypes.c_uint32(_VERSION))
    return max(0, version)


def confine(read: list[str], write: list[str], network: bool = True) -> None:
    """Confine this process (and everything it starts) to ``read`` and ``write``; raise OSError if that fails."""
    version = abi()
    if version < 1:
        raise OSError("Landlock is not available in this kernel")
    libc = _libc()
    handled = (1 << 13) - 1                                  # every right of version 1
    handled |= REFER if version >= 2 else 0
    handled |= TRUNCATE if version >= 3 else 0
    attr = _RulesetAttr(handled, NET_BIND_TCP | NET_CONNECT_TCP if version >= 4 and not network else 0)
    ruleset = libc.syscall(ctypes.c_long(_CREATE), ctypes.byref(attr), ctypes.c_size_t(ctypes.sizeof(attr)),
                           ctypes.c_uint32(0))
    if ruleset < 0:
        raise OSError(ctypes.get_errno(), "landlock_create_ruleset failed")
    try:
        for paths, rights in ((read, READ), (write, handled)):
            for path in paths:
                try:
                    fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
                except OSError:
                    continue                                 # not on this machine: nothing to allow
                try:
                    allowed = rights & handled
                    if not os.path.isdir(path):
                        allowed &= FILE_RIGHTS
                    rule = _PathBeneath(allowed, fd)
                    if libc.syscall(ctypes.c_long(_ADD_RULE), ctypes.c_int(ruleset), ctypes.c_int(_PATH_BENEATH),
                                    ctypes.byref(rule), ctypes.c_uint32(0)) < 0:
                        raise OSError(ctypes.get_errno(), f"landlock_add_rule failed for {path}")
                finally:
                    os.close(fd)
        if libc.prctl(_NO_NEW_PRIVS, 1, 0, 0, 0) < 0:
            raise OSError(ctypes.get_errno(), "prctl(PR_SET_NO_NEW_PRIVS) failed")
        if libc.syscall(ctypes.c_long(_RESTRICT), ctypes.c_int(ruleset), ctypes.c_uint32(0)) < 0:
            raise OSError(ctypes.get_errno(), "landlock_restrict_self failed")
    finally:
        os.close(ruleset)


def _check() -> int:
    """Start a confined child that may use one folder, and see that it cannot read another."""
    print(f"Landlock version {abi()}")
    with tempfile.TemporaryDirectory() as allowed, tempfile.TemporaryDirectory() as other:
        Path(other, "secret").write_text("not yours")
        probe = ("import sys, pathlib\n"
                 "pathlib.Path(sys.argv[1], 'note').write_text('ok')\n"
                 "try:\n    pathlib.Path(sys.argv[2], 'secret').read_text()\nexcept PermissionError:\n"
                 "    print('confined'); sys.exit(0)\n"
                 "print('NOT confined: read a file outside its folders'); sys.exit(1)\n")
        cmd = [sys.executable, "-I", str(HERE), "--write", allowed, "--no-network", "--",
               sys.executable, "-I", "-c", probe, allowed, other]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        print((result.stdout + result.stderr).strip())
        wrote = Path(allowed, "note").is_file()
        if not wrote:
            print("the confined process could not write in its own folder")
        return 0 if result.returncode == 0 and wrote else 1


def main(argv: list[str]) -> int:
    if argv == ["--check"]:
        return _check()
    read, write, sets, network = list(SYSTEM), list(DEVICES), {}, True
    args = list(argv)
    while args and args[0] != "--":
        flag = args.pop(0)
        if flag == "--no-network":
            network = False
        elif flag in ("--read", "--write", "--set") and args:
            value = args.pop(0)
            if flag == "--read":
                read.append(value)
            elif flag == "--write":
                write.append(value)
            else:
                name, _, value = value.partition("=")
                sets[name] = value
        else:
            print(f"sandbox: unexpected {flag!r}", file=sys.stderr)
            return 2
    cmd = args[1:]
    if not cmd:
        print("sandbox: no command given", file=sys.stderr)
        return 2
    try:
        confine(read, write, network)
    except OSError as exc:
        print(f"sandbox: cannot confine {cmd[0]}: {exc}", file=sys.stderr)
        return 126
    env = {k: v for k, v in os.environ.items() if not k.startswith("SPECDEV_")}
    env.update(sets)
    try:
        os.execvpe(cmd[0], cmd, env)
    except OSError as exc:
        print(f"sandbox: cannot run {cmd[0]}: {exc}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
