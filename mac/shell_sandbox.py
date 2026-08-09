"""A kernel-enforced jail for the one shell the reverse channel is allowed to start.

WHY THIS FILE EXISTS, stated as the failure it fixes rather than the feature it adds.

`mac/reverse_channel.scope_violation` reads the command text and refuses literal paths
that leave the workspace. That scan is real and it stays — it is what makes a refusal
legible ("out of scope: /etc/passwd is outside…") instead of a bare permission error.
But a scan of TEXT cannot contain a SHELL, and an audit proved it three ways:

    cat $HOME/.ssh/id_rsa          # the scan sees "$HOME/.ssh/id_rsa", the shell sees /Users/…
    cat ${HOME}/../outside/secret  # same, one brace later
    cat notes/link-to-elsewhere/x  # the scan sees a relative path; the kernel follows a symlink

All three returned file contents. Nothing that inspects the string before execution can
fix that class: expansion happens after the check, and a symlink resolves at open(2).
The only fence that sits where the escape lands is the kernel's, so this module puts one
there — `sandbox-exec` with a seatbelt profile that DENIES BY DEFAULT and re-allows
file reads and writes only under the realpath'd workspace root.

WHAT THE PROFILE ALLOWS, and why each line is there:

  * reading the system: /usr /bin /sbin /System /Library /opt/homebrew /opt/local
    /Applications, the dyld caches, and `/` itself (without a readable root directory
    even /bin/echo aborts before main). These are the binaries and libraries a command
    IS; denying them means denying every command.
  * a short literal list of shell startup files under /private/etc (zshrc, zprofile,
    paths, paths.d). Enough for `zsh -lc` to build a PATH, and nothing that carries
    user data — /etc/passwd, /etc/hosts and everything else under /etc stay denied.
  * `file-read-metadata` globally. stat(2) and readlink(2) on any path, contents on
    almost none: /usr/bin/git and /usr/bin/python3 are xcrun stubs that resolve
    /var/select/developer_dir and die without it. The leak this accepts is the
    EXISTENCE and size of files outside the workspace — `ls` of an outside directory
    is still denied, because listing a directory reads its data.
  * read AND write under the workspace root, and under one scratch directory used as
    TMPDIR (xcrun wants a cache file; without a writable TMPDIR every git invocation
    prints two errors). The scratch dir is the agent's own, not the workspace's, so a
    remote caller cannot pollute the folder Robin is working in with tool droppings.
  * the network only if `reverse_channel.sandbox_network` is turned on. Off by default:
    a jailed shell that can still POST is a jail with a mail slot. It is a config flag
    rather than a hard no because `git pull` inside the workspace is a legitimate thing
    to ask for from a phone, and it should be Robin's decision, made once, in writing.

WHAT IT STILL CANNOT DO. This contains FILE access. It does not stop a command inside
the workspace from burning CPU (the timeout does), from killing its own process group,
or from doing anything destructive to the workspace itself — that is what the
staged-confirm gate is for. Defence in depth means each fence answers one question.
"""
from __future__ import annotations

import os

from core import config

# The sandbox binary. Deprecated by Apple for a decade and still the only user-space
# seatbelt entry point that ships on every Mac. Absent means REFUSE, never means run
# uncontained — `available()` is checked in the reverse channel's guard, before the
# command is staged or run.
SANDBOX_EXEC = "/usr/bin/sandbox-exec"

# Scratch directory handed to the sandboxed shell as TMPDIR. Under the app's own
# support dir, in a subfolder of its own: the profile allows writes to THIS path only,
# so config.json and actions.jsonl next door stay unwritable.
SCRATCH_DIRNAME = "reverse-tmp"

# Read-only system paths. Everything a command needs to BE a command, and nothing that
# holds user data. /Users, /Volumes, /private/var (except the dyld and select stubs),
# /private/etc (except the shell rc files below) and /tmp are all absent on purpose.
SYSTEM_READ_SUBPATHS = (
    "/usr", "/bin", "/sbin", "/System", "/Library",
    "/opt/homebrew", "/opt/local", "/Applications",
    "/private/var/db/dyld", "/private/var/select",
)

# Shell startup files. Named one at a time rather than as `(subpath "/private/etc")`,
# because that subpath also contains /etc/passwd, /etc/hosts and /etc/ssh.
SHELL_RC_LITERALS = (
    "/private/etc/zshenv", "/private/etc/zprofile", "/private/etc/zshrc",
    "/private/etc/zlogin", "/private/etc/bashrc", "/private/etc/profile",
    "/private/etc/paths", "/private/etc/manpaths", "/private/etc/localtime",
)
# `path_helper`, run by /etc/zprofile, reads all four of these. Miss one and every
# single command answers with a "path_helper: … Operation not permitted" line stapled
# to its output — which is how this list got its last two entries.
SHELL_RC_SUBPATHS = ("/private/etc/paths.d", "/private/etc/manpaths.d")

DEVICE_LITERALS = (
    "/dev/null", "/dev/zero", "/dev/random", "/dev/urandom",
    "/dev/tty", "/dev/stdin", "/dev/stdout", "/dev/stderr",
)

# TLS and name resolution, added only when the network is switched on.
NETWORK_READ_LITERALS = ("/private/etc/hosts", "/private/etc/resolv.conf",
                         "/private/var/run/resolv.conf")
NETWORK_READ_SUBPATHS = ("/private/etc/ssl",)


class Unavailable(Exception):
    """No sandbox on this machine, or a root that cannot be jailed. Fail closed."""


def available() -> bool:
    return os.path.exists(SANDBOX_EXEC)


def scratch_dir() -> str:
    """The sandbox's TMPDIR, created on demand, owner-only."""
    path = os.path.join(config.SUPPORT_DIR, SCRATCH_DIRNAME)
    try:
        os.makedirs(path, exist_ok=True)
        os.chmod(path, 0o700)
    except OSError:
        return ""
    return os.path.realpath(path)


def _literal(path: str) -> str:
    escaped = str(path).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _rule(action: str, kind: str, paths) -> str:
    entries = " ".join(f"({kind} {_literal(p)})" for p in paths if p)
    return f"(allow {action} {entries})" if entries else ""


def profile(root: str, *, scratch: str = "", network: bool = False) -> str:
    """The seatbelt profile that jails one command to `root`.

    `root` must already be a realpath — the caller resolves it, and this refuses "/"
    and "" outright rather than emitting a profile that allows the whole disk. A jail
    whose walls are the edge of the world is not a jail, and generating one silently is
    the kind of mistake that only shows up in an audit.
    """
    resolved = (root or "").rstrip("/")
    if not resolved or root == "/":
        raise Unavailable("refusing to sandbox a shell to the whole filesystem")
    writable = [resolved] + ([scratch] if scratch else [])
    lines = [
        "(version 1)",
        "(deny default)",
        # Being a process: exec, fork, signal yourself, ask the kernel about itself.
        # None of these read a file; the file rules below are what contain the command.
        "(allow process-exec*)",
        "(allow process-fork)",
        "(allow file-map-executable)",
        "(allow signal (target self))",
        "(allow sysctl-read)",
        "(allow mach-lookup)",
        "(allow ipc-posix-shm*)",
        # stat/readlink anywhere; contents almost nowhere. See the module docstring.
        "(allow file-read-metadata)",
        _rule("file-read*", "literal", ("/",)),
        _rule("file-read*", "subpath", SYSTEM_READ_SUBPATHS),
        _rule("file-read*", "literal", SHELL_RC_LITERALS),
        _rule("file-read*", "subpath", SHELL_RC_SUBPATHS),
        _rule("file-read*", "literal", DEVICE_LITERALS),
        _rule("file-write-data", "literal",
              ("/dev/null", "/dev/tty", "/dev/stdout", "/dev/stderr")),
        # THE POINT OF THE FILE: the only place this shell may read a file's contents
        # or write one at all.
        _rule("file-read* file-write*", "subpath", writable),
    ]
    if network:
        lines += [
            "(allow network-outbound)",
            "(allow network-bind (local ip))",
            _rule("file-read*", "literal", NETWORK_READ_LITERALS),
            _rule("file-read*", "subpath", NETWORK_READ_SUBPATHS),
        ]
    return "\n".join(line for line in lines if line) + "\n"


def wrap(argv, root: str, *, scratch: str = "", network: bool = False) -> list:
    """`argv`, rewritten to run inside the jail. Raises `Unavailable` rather than
    returning the bare argv — a caller must never get an uncontained command back by
    accident."""
    if not available():
        raise Unavailable(f"{SANDBOX_EXEC} is missing")
    return [SANDBOX_EXEC, "-p", profile(root, scratch=scratch, network=network),
            *list(argv)]
