"""The reverse channel — phone-Pam acting ON Robin's Mac, over the tailnet.

Every other path in this system runs outward: the Mac talks to the model, the phone
talks to the model, both reach APIs. This one runs INWARD — something that is not on
this machine asks this machine to do things on it. That makes it the widest attack
surface in the build, so the shape of this file is defensive first and convenient
second, and every loosening below would need a reason written next to it.

WHAT HOLDS IT SHUT, in the order an attacker meets it:

 1. **OFF.** `reverse_channel.enabled` is false in `core/config.py` DEFAULTS and stays
    false until Robin turns it on. Nothing listens, and `serve()` refuses to start.
 2. **The tailnet, and only the tailnet.** `resolve_bind_host()` will bind a
    100.64.0.0/10 address and nothing else — not 0.0.0.0, not a LAN address, not
    loopback. If no Tailscale address can be found it refuses to start rather than
    falling back to something reachable. Requests from a non-tailnet peer are refused
    even so, because "we bound the right interface" is an assumption worth checking
    twice.
 3. **A shared token from the Keychain** (`reverse_channel_token`), compared with
    `hmac.compare_digest`. No token configured means every request is refused — an
    unconfigured secret must never mean an open door. There is deliberately NO rate
    limit (a lockout on an endpoint anyone on the tailnet can reach is a denial of
    service handed to them), so the token's LENGTH is the brute-force budget and
    `serve()` refuses to start with a short one.
 4. **A CLOSED allow-list of four operations** with a strict argument schema. An
    unknown operation, an unknown argument, or a missing one is refused and journalled;
    nothing is ever forwarded to a generic handler. The list is `OPERATIONS` and it is
    data you can read in one screen.
 5. **Workspace scope, enforced by the KERNEL — plus, for `open_file`, a TYPE fence.**
    `open_file` realpaths its target and refuses anything that does not land under the
    reverse-channel workspace, and then refuses anything that is not a view-safe
    document or image opened in a viewer this file names (see `open_refusal` /
    `open_argv`). Scope alone was not containment there and an audit proved it: the
    sandbox exists so `run_shell` can write INSIDE the workspace, so a caller wrote a
    `.command` in scope and asked `open_file` to open it, and LaunchServices ran it as
    Robin outside every jail. Where a file lives says nothing about what opening it
    does. `run_shell` gets the same text scan AND a `sandbox-exec` jail
    (`mac/shell_sandbox.py`) that denies file access by default and re-allows it only
    under the realpath'd root. The jail is not decoration: an audit of the text-scan-
    only version read `~/.ssh/id_rsa` through `$HOME`, read an outside file through
    `${HOME}/..`, and followed a symlink planted inside the workspace — every one of
    them a string the scan approved and the shell then expanded. A scan of text cannot
    contain a shell; only the thing that answers open(2) can. Missing `sandbox-exec`
    means run_shell is REFUSED, never run bare. Every command also starts its own shell
    at the root — there is no persistent shell here precisely so a `cd` in one call
    cannot move the next one out of scope.
 6. **The staged-confirm gate — the SAME one.** A mutating call does not run. It is
    staged in a `core.confirm_gate.PendingSlot`, which is the identical object
    `core.live_session.LiveSession` uses; there is exactly one implementation of "what
    counts as a yes" in this tree and `tests/test_reverse_channel.py` asserts it.
 7. **The screenshot denylist is `mac.macos_context`'s**, read not rewritten, including
    its fail-closed behaviour when the frontmost app cannot be identified — and then a
    second, narrower fence this surface adds: an ALLOW-list of apps a phone may see
    (`SCREENSHOT_ALLOWED_APPS`). A denylist says no to what someone thought of and yes
    to everything unheard of; on this Mac, with Robin watching, that is a fair default,
    but a bank nobody listed is not a thing to ship down a wire on the strength of
    never having been named.
 8. **Everything journals** to `actions.jsonl` — the call, the refusal, the staging,
    the confirmation and the words that were offered as consent. Results that are
    CONTENT rather than outcome (the screen, a screenshot) are recorded as a length:
    an audit trail is not a place to accumulate a second copy of what was seen. An
    unhandled exception is caught, journalled and answered with a flat 500, so a
    traceback never travels back down the wire.

THE ONE THING THIS CANNOT DO, stated plainly because pretending otherwise would be
worse than the limitation itself: Robin is not at this Mac when he uses the reverse
channel — that is the entire point — so the confirmation cannot be collected here. It
arrives as a `transcript` on a separate authenticated request, relayed by whatever is
holding the conversation. The gate still decides deterministically (an allow-list of
complete affirmations, a TTL, a handle bound to the one staged action), and the
transcript is journalled verbatim, so a fabricated "yes" is a recorded fabrication with
a name and a timestamp on it. But it is a RELAYED consent, not a witnessed one. That is
the residual risk of letting a phone drive a laptop, and it is why the channel is off
by default and scoped to one folder.

RUN IT / KILL IT

    python3 -m mac.reverse_channel            # start (refuses unless enabled)
    python3 -m mac.reverse_channel --kill     # stop, one command
    python3 -m mac.reverse_channel --status   # is it up, and where
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable

from core import audit, capabilities, config, confirm_gate, destructive
from mac import shell_sandbox

# Which capability profile this surface runs. NOT named `SURFACE_PROFILE`: that symbol
# is the drift checker's protocol for "this directory is a conversational surface with
# a tool list", and this file is neither — `mac/caps_install.py` already speaks for the
# Mac surface, and two declarations in one directory is a hard error there.
REVERSE_PROFILE = "mac_reverse"

# Tailscale hands out CGNAT space. Anything outside it is not the tailnet.
TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")

# Where the `tailscale` binary lives on a Mac, in the order worth trying. PATH is
# consulted last so a shadowed binary in a user directory cannot decide what counts as
# a tailnet address.
TAILSCALE_BINARIES = (
    "/usr/local/bin/tailscale",
    "/opt/homebrew/bin/tailscale",
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
)

PIDFILE = "reverse-channel.pid"

# A request body big enough for a real command and nothing like big enough to be a
# payload. The channel takes instructions, never data.
MAX_BODY_BYTES = 64 * 1024
MAX_COMMAND_CHARS = 4000
MAX_TRANSCRIPT_CHARS = 400
MAX_OUTPUT_CHARS = 6000

# There is no rate limit on the endpoint, so the token's length is the brute-force
# budget. Checked at startup, where refusing costs nothing.
MIN_TOKEN_CHARS = 24


class Refused(Exception):
    """A request that will not be served, with the HTTP status and the journal event.

    Every refusal path raises this rather than returning a sentinel, so there is no way
    to fall through a check into the dispatcher by forgetting a `return`.
    """

    def __init__(self, reason: str, status: int = 400, event: str = "refused") -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.event = event


class Disabled(Exception):
    """The channel is off in config. Raised by `serve()` so starting it is a decision."""


# --- where it may listen --------------------------------------------------------------

def tailscale_addresses(runner: Callable | None = None) -> list:
    """Every IPv4 address `tailscale ip -4` reports for this machine.

    A list, not a single address: a machine can hold more than one, and the caller
    decides. Errors are swallowed into an empty list — "no tailnet address" is the
    fail-closed answer for both "Tailscale is down" and "Tailscale is not installed".
    """
    run = runner or subprocess.run
    for binary in (*TAILSCALE_BINARIES, "tailscale"):
        try:
            result = run([binary, "ip", "-4"], capture_output=True, text=True, timeout=5)
        except Exception:
            continue
        if getattr(result, "returncode", 1) != 0:
            continue
        found = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
        if found:
            return found
    return []


def is_tailnet_address(candidate: str) -> bool:
    try:
        return ipaddress.ip_address(str(candidate)) in TAILNET_V4
    except ValueError:
        return False


def resolve_bind_host(candidate: str | None = None, runner: Callable | None = None) -> str:
    """The one address this may bind, or `Refused`.

    `candidate` (from config, if Robin pinned one) still has to BE a tailnet address —
    pinning is allowed to narrow the choice, never to widen it. With no candidate the
    first address Tailscale reports wins. There is deliberately no fallback: a reverse
    channel that cannot find the tailnet must not quietly bind something else, and
    "0.0.0.0" is refused by the same rule that refuses 127.0.0.1 rather than by a
    special case that could be edited away.
    """
    addresses = tailscale_addresses(runner)
    if candidate:
        if not is_tailnet_address(candidate):
            raise Refused(f"{candidate!r} is not a Tailscale address (100.64.0.0/10); "
                          f"the reverse channel binds the tailnet or nothing", 403)
        if addresses and candidate not in addresses:
            raise Refused(f"{candidate!r} is not an address this machine holds on the "
                          f"tailnet ({', '.join(addresses)})", 403)
        return candidate
    for address in addresses:
        if is_tailnet_address(address):
            return address
    raise Refused("no Tailscale address on this machine — refusing to bind anything "
                  "else. Bring Tailscale up first.", 403)


# --- workspace scope ------------------------------------------------------------------

def workspace_root() -> str:
    """The one folder the reverse channel may act in, fully resolved.

    `realpath`, so a symlinked workspace is compared against where it actually lands —
    otherwise every containment check below could be satisfied by a link.
    """
    configured = config.get("reverse_channel.workspace") or \
        config.REVERSE_CHANNEL_DEFAULT_WORKSPACE
    return os.path.realpath(os.path.expanduser(configured))


def _inside(path: str, root: str) -> bool:
    resolved = os.path.realpath(path)
    return resolved == root or resolved.startswith(root + os.sep)


def path_violation(path: str, root: str) -> str | None:
    """Why this path is out of scope, or None. Used by `open_file` and by the scan."""
    if not path:
        return "no path given"
    expanded = os.path.expanduser(path)
    absolute = expanded if os.path.isabs(expanded) else os.path.join(root, expanded)
    if not _inside(absolute, root):
        return f"{path} is outside the reverse-channel workspace ({root})"
    return None


# Tokens that are never allowed regardless of paths: they either leave the user Robin
# authorised or hand the command to something this scan cannot follow.
FORBIDDEN_TOKENS = frozenset({"sudo", "su", "doas", "ssh", "scp", "rsync", "osascript"})

# Shell operators shlex leaves attached to the token beside them.
_SEPARATORS = re.compile(r"[;&|()<>]+")


def scope_violation(command: str, root: str) -> str | None:
    """Why this command is out of scope, or None.

    A LITERAL-PATH scan, and it is important to be honest about what that is and is
    not. It refuses: privilege escalation and remote-execution binaries; any absolute
    or `~` path that does not land under the workspace; any relative path that climbs
    out with `..`. It cannot see through a variable, a command substitution, or a
    symlink created after the check — nothing short of a sandbox can. Scope is the
    FIRST fence here, not the only one: a destructive command still stages behind the
    confirm gate, and the whole channel is off unless Robin turned it on.
    """
    if not (command or "").strip():
        return "empty command"
    if len(command) > MAX_COMMAND_CHARS:
        return f"command is longer than {MAX_COMMAND_CHARS} characters"
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError as error:
        # Unbalanced quotes: the scan cannot see the real tokens, so it refuses rather
        # than guessing at them.
        return f"could not be parsed ({error}) — refusing what cannot be read"
    for token in tokens:
        # shlex keeps shell operators glued to their neighbours (`cd ..;` is ONE token),
        # so a path can hide behind a separator. Split them back off before looking.
        for piece in _SEPARATORS.split(token):
            bare = piece.strip()
            if not bare:
                continue
            if bare.lower() in FORBIDDEN_TOKENS:
                return f"{bare} is not available over the reverse channel"
            # `--flag=/path` hides the path from a leading-character test: check both.
            for part in ([bare] if "=" not in bare else [bare, bare.split("=", 1)[1]]):
                if not part:
                    continue
                if part.startswith(("/", "~")) or ".." in part.split("/"):
                    violation = path_violation(part, root)
                    if violation:
                        return violation
    return None


# --- the operations ---------------------------------------------------------------------
# Mutation classes. `CLASSIFY` means "ask core.destructive" — a read runs, a write stages.
NEVER, ALWAYS, CLASSIFY = "never", "always", "classify"


@dataclass(frozen=True)
class Operation:
    """One allowed operation. The schema is exact: a missing required argument or an
    argument nobody declared is a refusal, not a shrug. A remote caller does not get to
    teach this surface new parameters."""

    name: str
    mutating: str
    required: tuple
    run: Callable                   # (channel, args) -> str
    guard: Callable | None = None   # (channel, args) -> None, raises Refused
    # True when the RESULT is content rather than an outcome — the text on Robin's
    # screen, the pixels of his window. `actions.jsonl` is an accountability record of
    # what was DONE, not a copy of what was seen: a live run put 300 characters of
    # base64 JPEG and 300 characters of whatever window was open into it, which is a
    # second transcript growing inside the one file that is supposed to be reviewable.
    # These operations journal a length instead. The caller still gets the content.
    opaque_result: bool = False


def _op_run_shell(channel: "ReverseChannel", args: dict) -> str:
    """One command, one fresh shell, rooted at the workspace and JAILED to it.

    Deliberately NOT `core.shell.Shell`: that one is persistent so a live conversation
    can `cd` into a project and stay there, which is exactly the property a scoped
    remote executor must not have. Here every call starts at the workspace root, so
    scope is re-established by construction on each command rather than trusted to
    survive the previous one.

    The containment is `mac.shell_sandbox`'s, not this function's, and that split is
    the correction an audit forced: `scope_violation` reads the command TEXT, and text
    cannot contain a shell — `$HOME`, `${HOME}`, `$(…)` and a symlink all resolve after
    the scan has finished and passed. The seatbelt profile denies by default and
    re-allows file access only under the realpath'd root, so the escape lands on a
    kernel refusal instead of on Robin's ssh key. The text scan stays because it turns
    those attempts into a legible "out of scope" answer instead of a bare EPERM.
    """
    command = args["command"]
    binary = next((b for b in capabilities.get(REVERSE_PROFILE)["shell_binaries"]
                   if os.path.exists(b)), "/bin/zsh")
    timeout = float(config.get("reverse_channel.shell_timeout", 20) or 20)
    scratch = shell_sandbox.scratch_dir()
    env = config.subprocess_env()
    if scratch:
        # Give the jail a writable TMPDIR of its own. Without one, xcrun's stubs (git,
        # python3) print two "couldn't create cache file" errors over every result.
        env["TMPDIR"] = scratch
    try:
        argv = shell_sandbox.wrap(
            [binary, "-lc", command], channel.root, scratch=scratch,
            network=bool(config.get("reverse_channel.sandbox_network", False)))
    except shell_sandbox.Unavailable as error:
        # Belt and braces: `_guard_shell_scope` already refused this before staging.
        # Reaching here means the machine changed under a staged action, and an
        # uncontained fallback is the one answer that must not exist.
        return f"error: refusing to run uncontained ({error})"
    try:
        result = subprocess.run(
            argv,
            capture_output=True, text=True, timeout=timeout,
            cwd=channel.root, env=env,
            start_new_session=True)
    except subprocess.TimeoutExpired:
        return f"(timed out after {int(timeout)}s and was killed)"
    except Exception as error:                      # noqa: BLE001 — reported, never raised
        return f"error: {error}"
    out = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        out += f"\n(exit {result.returncode})"
    return out[:MAX_OUTPUT_CHARS] or "(no output)"


def _guard_shell_scope(channel: "ReverseChannel", args: dict) -> None:
    """No sandbox, no shell. Then the text scan, for a legible refusal.

    The sandbox check comes FIRST and refuses rather than degrading, because "the jail
    is missing so we ran it anyway" is the only outcome this surface must never have.
    It runs again at confirm time (the guards are re-run there), so a staged command
    cannot execute uncontained on a machine that lost `sandbox-exec` in between.
    """
    if not shell_sandbox.available():
        raise Refused("the shell sandbox (sandbox-exec) is not on this machine — "
                      "refusing to run a remote command uncontained", 403,
                      "refused_no_sandbox")
    root = channel.root
    if not root or root == "/":
        raise Refused("the reverse-channel workspace resolves to the whole filesystem "
                      "— refusing to run anything in it", 403, "refused_no_sandbox")
    violation = scope_violation(args.get("command", ""), root)
    if violation:
        raise Refused(f"out of scope: {violation}", 400, "refused_scope")


# --- what `open_file` is allowed to be --------------------------------------------------
# An audit walked through the front door here, and it did it without breaking either
# fence — it composed them. `run_shell` is jailed to the workspace, and WRITING INSIDE
# the workspace is precisely what that jail is for, so a caller writes `evil.command`
# in scope (allowed, by design) and then asks `open_file` to open it. `open` cannot be
# sandboxed — it asks the window server to launch something as Robin, so a jail around
# `open` would contain the wrong process — and LaunchServices does not open a
# `.command`, it RUNS it, as Robin, outside every jail. The canary landed outside the
# workspace. Containing WHERE a file lives says nothing about WHAT opening it does, and
# the spoken preview ("about to open evil.command") read like a document, not like code.
#
# So this operation no longer dispatches on file type at all. Two locks, both closed by
# default:
#
#   * a TYPE allow-list — a file is opened only if its extension is one of the viewing
#     formats below, plus a regular-file check, plus a refusal of anything carrying the
#     execute bit, plus a refusal of anything living inside a bundle directory. Absent
#     on purpose: `.command`/`.sh`/`.scpt`/`.applescript`/`.terminal`/`.workflow`/`.app`
#     (run outright), `.webloc`/`.url` (navigate somewhere on Robin's behalf), `.html`
#     (a local page runs JavaScript and can read its file:// neighbours), `.rtf`/`.docx`
#     (rich parsers, macros), and every extensionless file. Source code is absent too —
#     `run_shell` can `cat` it, and an editor opening a project is a bigger machine than
#     this operation needs to be.
#   * a FIXED viewer — the argv below names the application, so LaunchServices never
#     gets to pick a handler for the file. Even a mislabelled file therefore lands in a
#     text editor or in Preview rather than in whatever claims its type.
#
# The target handed to `open` is always an absolute realpath, so it cannot be read as a
# flag, and both locks are checked TWICE: in the guard (which runs again at confirm
# time) and again inside `_op_open_file` on the realpath it is about to hand over.
#
# What is left, said plainly: `-t` opens Robin's default TEXT editor, whichever he has
# set, and `-a Preview` opens Preview. Neither one runs the document it is given — that
# is the whole point of naming them — but this operation's safety does rest on those two
# applications being document viewers, which is a much smaller assumption than trusting
# LaunchServices to pick a handler for a file an attacker named.
VIEWABLE_TEXT_EXTENSIONS = frozenset({
    ".txt", ".md", ".markdown", ".rst", ".log",
    ".json", ".csv", ".tsv", ".yaml", ".yml", ".toml", ".ini", ".conf",
})
VIEWABLE_IMAGE_EXTENSIONS = frozenset({
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".tiff", ".tif", ".bmp", ".heic",
})
VIEWABLE_EXTENSIONS = VIEWABLE_TEXT_EXTENSIONS | VIEWABLE_IMAGE_EXTENSIONS

# Directory suffixes macOS treats as one openable object. A file INSIDE one of these is
# refused even when its own extension is harmless: the interesting question there is not
# what the leaf is, it is why a phone is reaching into a bundle at all.
BUNDLE_SUFFIXES = (".app", ".workflow", ".scptd", ".rtfd", ".bundle", ".pkg",
                   ".framework", ".download", ".prefpane", ".service", ".qlgenerator")


def open_refusal(target: str, root: str) -> str | None:
    """Why this file will not be handed to the window server, or None.

    `target` is an already-realpath'd absolute path known to be inside `root`; this
    function answers the second question — not "where is it" but "what is it".
    """
    name = os.path.basename(target)
    extension = os.path.splitext(name)[1].lower()
    if extension not in VIEWABLE_EXTENSIONS:
        return (f"{name} is not a type this channel opens. It shows documents and "
                f"images for VIEWING ({', '.join(sorted(VIEWABLE_EXTENSIONS))}) and "
                f"refuses anything a handler could run")
    relative = os.path.relpath(target, root)
    for component in relative.split(os.sep):
        if component.lower().endswith(BUNDLE_SUFFIXES):
            return f"{name} lives inside the bundle {component}"
    try:
        info = os.stat(target)
    except OSError as error:
        return f"{name} could not be read ({error.strerror or error})"
    if not stat.S_ISREG(info.st_mode):
        return f"{name} is not a regular file"
    if info.st_mode & 0o111:
        return f"{name} has the execute bit set — a runnable file is never opened here"
    return None


def open_argv(target: str) -> list:
    """The FIXED viewer for this file — never the handler LaunchServices would choose.

    `-a Preview` / `-t` (the default TEXT editor) both mean "show me this", and neither
    consults the file about what should happen to it. Naming the application is the
    difference between opening a document and running one.
    """
    extension = os.path.splitext(target)[1].lower()
    if extension in VIEWABLE_IMAGE_EXTENSIONS:
        return ["/usr/bin/open", "-a", "Preview", target]
    return ["/usr/bin/open", "-t", target]


def _op_open_file(channel: "ReverseChannel", args: dict) -> str:
    """Show one in-scope, view-safe file in a viewer this function picks.

    NOT sandboxed, and it cannot be, which is why containment here is a pair of checks
    rather than a jail — and why both of them are made TWICE, once in the guard and once
    here on the realpath about to be handed over. The gap between the two is where a
    symlink swapped after the guard would land, or a `chmod +x` applied while Robin was
    saying yes, and staging (open_file always stages) makes that gap as wide as a spoken
    confirmation.
    """
    target = os.path.realpath(
        os.path.join(channel.root, os.path.expanduser(args["path"])))
    if not _inside(target, channel.root):
        # Journalled as the outcome of a call rather than raised: by here the action was
        # confirmed, and "it was refused at the last moment" is a thing the record has
        # to keep.
        return (f"error: refused — {args['path']} resolves outside the workspace "
                f"({channel.root}) at the moment of opening it")
    refusal = open_refusal(target, channel.root)
    if refusal:
        return (f"error: refused — {refusal}, checked again at the moment of "
                f"opening it")
    result = subprocess.run(open_argv(target),
                            capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        return f"error: {(result.stderr or '').strip() or 'open failed'}"
    return f"opened {args['path']}"


def _guard_open_scope(channel: "ReverseChannel", args: dict) -> None:
    path = args.get("path", "")
    violation = path_violation(path, channel.root)
    if violation:
        raise Refused(f"out of scope: {violation}", 400, "refused_scope")
    target = os.path.realpath(os.path.join(channel.root, os.path.expanduser(path)))
    if not os.path.exists(target):
        raise Refused(f"{path} does not exist", 400, "refused_missing")
    refusal = open_refusal(target, channel.root)
    if refusal:
        raise Refused(f"will not open that: {refusal}", 400, "refused_open_type")


def _op_grab_context(channel: "ReverseChannel", args: dict) -> str:
    from mac import macos_context
    return macos_context.grab_context()


def _op_screenshot(channel: "ReverseChannel", args: dict) -> str:
    from mac import macos_context
    shot = macos_context.grab_window_screenshot()
    if not shot:
        # `grab_window_screenshot` returns "" for a denied window AND for a missing
        # Screen Recording grant. The guard below has already ruled out the denylist,
        # so this is the permission case — say so instead of returning an empty string
        # the caller would have to guess about.
        return "(no screenshot — the window could not be captured; check Screen Recording)"
    return shot


# Apps this surface may photograph, matched on the frontmost app's EXACT name, cased
# down. A second fence in front of `macos_context`'s denylist, and the reason it exists
# is a hole an audit found in the denylist MODEL rather than in its contents: a denylist
# answers "no" for the apps someone thought of, and "yes" for every app nobody has met
# yet. Locally, where Robin is at the keyboard and can see the capture happen, that is a
# reasonable default. Down a wire to a phone it is not — a bank the list has never heard
# of, a health portal in a browser whose title says nothing, a new messenger, all
# captured and shipped off the machine.
#
# So: work surfaces only, and an app that is not on this list is refused whether or not
# anyone has ever called it sensitive. `reverse_channel.screenshot_apps` replaces the
# list for Robin, which is how a new editor gets added — deliberately, in config, not by
# being unheard of.
SCREENSHOT_ALLOWED_APPS = frozenset({
    "terminal", "iterm2", "ghostty", "warp", "alacritty", "kitty",
    "code", "visual studio code", "cursor", "zed", "sublime text", "xcode",
    "finder", "preview", "textedit", "notes",
})


def screenshot_allowlist() -> frozenset:
    configured = config.get("reverse_channel.screenshot_apps")
    if isinstance(configured, (list, tuple)) and configured:
        return frozenset(str(a).strip().lower() for a in configured if str(a).strip())
    return SCREENSHOT_ALLOWED_APPS


def _guard_screenshot(channel: "ReverseChannel", args: dict) -> None:
    """Two fences, in this order: `macos_context`'s denylist, then this surface's
    allow-list.

    The denylist is asked FIRST and unchanged — the policy stays in `macos_context`,
    read not rewritten, including its fail-closed answer when the frontmost app cannot
    be identified. Asking it first also means a denylisted app is refused as
    denylisted, which is the more useful thing to read in the journal.

    The allow-list is then asked as a second, narrower question that only this surface
    poses: is this an app Robin has said a PHONE may see? An unknown app fails closed
    here, which is the property a denylist structurally cannot provide.
    """
    from mac import macos_context
    app, title = macos_context._frontmost_app_and_title()
    if not app and not title:
        raise Refused("the frontmost app could not be identified — refusing to capture",
                      403, "refused_screenshot_unknown_app")
    try:
        _window, cursor_title = macos_context._ax_window_under_cursor()
    except Exception:                               # noqa: BLE001 — absence, not failure
        cursor_title = ""
    if macos_context.screenshot_denied(app, title, cursor_title):
        raise Refused("that window is on the screenshot denylist",
                      403, "refused_screenshot_denied")
    if (app or "").strip().lower() not in screenshot_allowlist():
        raise Refused(f"{app or 'that window'} is not on the reverse channel's "
                      f"screenshot allow-list — an app this surface has never been told "
                      f"about is not captured down a wire",
                      403, "refused_screenshot_not_allowed")


OPERATIONS = {
    "run_shell": Operation("run_shell", CLASSIFY, ("command",), _op_run_shell,
                           _guard_shell_scope),
    "open_file": Operation("open_file", ALWAYS, ("path",), _op_open_file,
                           _guard_open_scope),
    "grab_context": Operation("grab_context", NEVER, (), _op_grab_context,
                              opaque_result=True),
    "screenshot": Operation("screenshot", NEVER, (), _op_screenshot, _guard_screenshot,
                            opaque_result=True),
}


def _for_journal(op: Operation, result: str) -> str:
    """What the accountability record keeps: the outcome, or a length when the result
    is content the record has no business copying."""
    if op.opaque_result:
        return f"<{len(result or '')} chars, not recorded>"
    return result


def validate_args(op: Operation, raw: dict) -> dict:
    """Exactly the declared arguments, all of them strings, nothing else."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise Refused("args must be an object", 400, "refused_args")
    unknown = sorted(set(raw) - set(op.required))
    if unknown:
        raise Refused(f"{op.name} does not take {', '.join(unknown)}", 400, "refused_args")
    args = {}
    for field in op.required:
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            raise Refused(f"{op.name} needs a non-empty {field}", 400, "refused_args")
        args[field] = value
    return args


# --- the channel ------------------------------------------------------------------------

class ReverseChannel:
    """The dispatcher, independent of HTTP so it can be tested as what it is: a policy.

    Holds the ONE `confirm_gate.PendingSlot`. A lock wraps every touch of it because
    HTTP handlers can interleave and stage→read-back→confirm must not.
    """

    def __init__(self, root: str | None = None) -> None:
        self._root = root
        self.gate = confirm_gate.PendingSlot()
        self._lock = threading.Lock()

    @property
    def root(self) -> str:
        """Read live, not snapshotted: if Robin narrows the workspace, the next call is
        narrowed — including a call staged before the change and confirmed after it,
        because the guards run again at execution time.

        Always a realpath. The sandbox profile and the path checks both compare against
        this string, and a root that still contains a symlink would let one of them
        disagree with the kernel about where the workspace actually is.
        """
        return os.path.realpath(self._root) if self._root else workspace_root()

    # -- journal ------------------------------------------------------------------
    def _journal(self, event: str, tool: str, args=None, result=None,
                 request_id: str = "") -> None:
        audit.record(event, tool, args, result, surface=REVERSE_PROFILE,
                     session=request_id)

    # -- dispatch -----------------------------------------------------------------
    def call(self, op_name, raw_args, *, request_id: str = "") -> dict:
        """Run, or stage, or refuse. There is no fourth outcome."""
        op = OPERATIONS.get(op_name) if isinstance(op_name, str) else None
        if op is None:
            # Journalled with the name that was asked for, so a probe for `eval` or
            # `put_text` leaves a trace instead of a 400 nobody ever sees.
            self._journal("refused_unknown_op", str(op_name)[:60] or "(unnamed)",
                          {}, "refused", request_id)
            raise Refused(f"unknown operation {op_name!r}; this surface has "
                          f"{', '.join(sorted(OPERATIONS))} and nothing else",
                          400, "refused_unknown_op")
        try:
            args = validate_args(op, raw_args)
            if op.guard:
                op.guard(self, args)
        except Refused as refusal:
            self._journal(refusal.event, op.name, raw_args if isinstance(raw_args, dict) else {},
                          f"refused: {refusal.reason}", request_id)
            raise
        if not self._is_mutating(op, args):
            result = op.run(self, args)
            self._journal("call", op.name, args, _for_journal(op, result), request_id)
            return {"status": "ok", "result": result}
        return self._stage(op, args, request_id)

    def _is_mutating(self, op: Operation, args: dict) -> bool:
        if op.mutating == NEVER:
            return False
        if op.mutating == ALWAYS:
            return True
        # CLASSIFY: `core.destructive` is the same classifier the Mac's own run_shell
        # gate uses, so "what counts as destructive" has one definition here too.
        return destructive.classify(args.get("command", "")).destructive

    def _stage(self, op: Operation, args: dict, request_id: str) -> dict:
        with self._lock:
            staged = self.gate.stage(op.name, args)
        if staged.status == confirm_gate.REFUSED:
            busy = staged.pending
            preview = confirm_gate.confirmation_preview(
                busy["tool"], busy["args"], capabilities.shell_host(REVERSE_PROFILE))
            self._journal("refused_gate_busy", op.name, args,
                          f"blocked by {busy['tool']}", request_id)
            raise Refused(f"NOT STAGED AND NOT RUN — an action is already awaiting "
                          f"confirmation: {preview}. It has to be answered before "
                          f"anything else can be staged.", 409, "refused_gate_busy")
        if staged.displaced:
            expired = staged.displaced
            self._journal("expired", expired["tool"], expired["args"],
                          "not executed", request_id)
        preview = confirm_gate.confirmation_preview(
            op.name, args, capabilities.shell_host(REVERSE_PROFILE))
        self._journal("staged", op.name, args, preview, request_id)
        return {
            "status": "staged",
            "id": staged.action_id,
            "preview": preview,
            "instruction": ("NOTHING HAS RUN. Read the preview out loud, word for word, "
                            "and send back exactly what he says next as `transcript` on "
                            "/confirm with this id. Do not paraphrase it and do not "
                            "answer on his behalf — a plain 'yes' or 'do it' is what "
                            "runs this, anything else drops it."),
        }

    def pending(self) -> dict:
        with self._lock:
            current, action_id = self.gate.current, self.gate.current_id
        if not current:
            return {"status": "idle"}
        return {
            "status": "staged",
            "id": action_id,
            "tool": current["tool"],
            "preview": confirm_gate.confirmation_preview(
                current["tool"], current["args"],
                capabilities.shell_host(REVERSE_PROFILE)),
            "age_s": round(time.time() - current["ts"], 1),
        }

    def confirm(self, action_id, transcript, *, request_id: str = "") -> dict:
        """Resolve the one staged action against the words that were relayed.

        The decision is `core.confirm_gate`'s, unchanged and unhelped: this method
        cannot make a "yes" out of something that is not one, and the id binds the
        answer to the action the caller was actually shown.
        """
        if not isinstance(transcript, str) or not transcript.strip():
            raise Refused("confirm needs the transcript of what he said", 400,
                          "refused_args")
        if len(transcript) > MAX_TRANSCRIPT_CHARS:
            raise Refused("that is not a confirmation, it is a paragraph", 400,
                          "refused_args")
        if not isinstance(action_id, str) or not action_id.strip():
            raise Refused("confirm needs the id of the staged action", 400, "refused_args")
        with self._lock:
            outcome, pending = self.gate.resolve(transcript, action_id=action_id)
        if pending is None:
            raise Refused("nothing is awaiting confirmation", 409, "refused_no_pending")
        # The transcript is journalled VERBATIM (no field of that name is redacted, on
        # purpose): it is the evidence of what was offered as consent, and a redacted
        # consent record answers nothing weeks later.
        record = dict(pending["args"])
        record["transcript"] = transcript
        if outcome == "mismatch":
            self._journal("refused_stale_confirm", pending["tool"], record,
                          "not executed", request_id)
            raise Refused("that id is not the action awaiting confirmation", 409,
                          "refused_stale_confirm")
        if outcome != "confirmed":
            self._journal(outcome, pending["tool"], record, "not executed", request_id)
            return {"status": outcome, "result": None}
        op = OPERATIONS[pending["tool"]]
        try:
            # Re-guard at execution time. The args were checked when they were staged,
            # but the workspace can be narrowed between staging and confirming, and the
            # narrower answer has to win.
            if op.guard:
                op.guard(self, pending["args"])
        except Refused as refusal:
            self._journal("refused_scope_on_confirm", op.name, record,
                          f"refused: {refusal.reason}", request_id)
            raise
        result = op.run(self, pending["args"])
        self._journal("confirmed", op.name, record, _for_journal(op, result), request_id)
        return {"status": "confirmed", "result": result}


# --- HTTP ---------------------------------------------------------------------------

def token() -> str | None:
    return config.secret("reverse_channel_token")


def token_ok(candidate: str | None) -> bool:
    """No configured token means NO. An unset secret is not an open door."""
    expected = token()
    if not expected or not candidate:
        return False
    return hmac.compare_digest(str(candidate), str(expected))


def enabled() -> bool:
    return bool(config.get("reverse_channel.enabled", False))


# The routes, as DATA: (method, path) -> how to answer. A request that is not in this
# table gets a 404 and a journal line; there is no catch-all handler to fall into.
def _route_op(channel: "ReverseChannel", body: dict, request_id: str) -> dict:
    return channel.call(body.get("op"), body.get("args"), request_id=request_id)


def _route_confirm(channel: "ReverseChannel", body: dict, request_id: str) -> dict:
    return channel.confirm(body.get("id"), body.get("transcript"), request_id=request_id)


def _route_pending(channel: "ReverseChannel", body: dict, request_id: str) -> dict:
    return channel.pending()


def _route_health(channel: "ReverseChannel", body: dict, request_id: str) -> dict:
    return {"status": "ok", "workspace": channel.root,
            "operations": sorted(OPERATIONS),
            "pending": channel.pending()["status"]}


ROUTES = {
    ("POST", "/op"): _route_op,
    ("POST", "/confirm"): _route_confirm,
    ("GET", "/pending"): _route_pending,
    ("GET", "/health"): _route_health,
}


def handle(channel: "ReverseChannel", method: str, path: str, headers, body: bytes,
           peer: str) -> tuple:
    """One request in, `(status, payload)` out — the whole HTTP policy, socket-free.

    Written as a pure function so the refusal paths can be tested as decisions rather
    than as a live server. `serve_forever` below is a thin socket wrapper over it.
    """
    if not enabled():
        return 403, {"status": "refused", "reason": "the reverse channel is disabled"}
    # Defence in depth: we bind the tailnet (`resolve_bind_host`), so a peer from
    # anywhere else means an assumption broke somewhere upstream. Refuse rather than
    # trust the bind.
    if not is_tailnet_address(peer):
        channel._journal("refused_offnet", "-", {"peer": peer}, "refused")
        return 403, {"status": "refused", "reason": "not a tailnet peer"}
    if not token_ok(_header(headers, "x-reverse-token")):
        channel._journal("refused_unauthorized", "-", {"peer": peer}, "refused")
        return 401, {"status": "refused", "reason": "unauthorized"}
    route = ROUTES.get((method.upper(), path.split("?", 1)[0]))
    if route is None:
        channel._journal("refused_unknown_route", f"{method} {path}"[:60], {}, "refused")
        return 404, {"status": "refused", "reason": "no such endpoint"}
    request_id = uuid.uuid4().hex
    try:
        return 200, route(channel, _parse_body(body), request_id)
    except Refused as refusal:
        return refusal.status, {"status": "refused", "reason": refusal.reason}
    except Exception as error:                  # noqa: BLE001 — contained on purpose
        # An unhandled exception would otherwise reach BaseHTTPRequestHandler, which
        # answers with a traceback and writes nothing to the journal: a stack trace
        # handed to a remote caller, and an action with no record. Both are worse than
        # a flat 500.
        channel._journal("error", str(op_or(body)), {}, f"{type(error).__name__}",
                         request_id)
        from core import caps
        caps.log(f"reverse channel: unhandled {error!r}")
        return 500, {"status": "error", "reason": "the operation failed"}


def op_or(body) -> str:
    """The operation name a failed request was asking for, for the journal line."""
    if isinstance(body, (bytes, str)):
        try:
            body = json.loads(body or b"{}")
        except ValueError:
            return "-"
    return str((body or {}).get("op", "-"))[:60] if isinstance(body, dict) else "-"


def _header(headers, name: str) -> str | None:
    """Case-insensitive lookup that works for a dict or an email.Message."""
    getter = getattr(headers, "get", None)
    if getter is None:
        return None
    return headers.get(name) or headers.get(name.title()) or headers.get(name.upper())


def _parse_body(body: bytes) -> dict:
    if body is None:
        return {}
    if len(body) > MAX_BODY_BYTES:
        raise Refused("body too large", 413, "refused_args")
    if not body.strip():
        return {}
    try:
        parsed = json.loads(body)
    except ValueError:
        raise Refused("body is not JSON", 400, "refused_args") from None
    if not isinstance(parsed, dict):
        raise Refused("body must be a JSON object", 400, "refused_args")
    return parsed


def build_server(channel: "ReverseChannel", host: str, port: int):
    """A `ThreadingHTTPServer` wired to `handle`.

    The stdlib, deliberately: `mac/` is copied into the .app verbatim without an import
    scan, so every third-party module reached from here has to be shipped by hand
    (tests/test_bundle_deps.py enforces it). A remote-control endpoint is also the last
    place to want an ASGI stack's worth of code it did not audit — four JSON endpoints
    do not need a framework.
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        server_version = "ThrivbeReverse/1"
        sys_version = ""

        def _answer(self, method: str) -> None:
            length = int(self.headers.get("content-length") or 0)
            if length > MAX_BODY_BYTES:
                self._send(413, {"status": "refused", "reason": "body too large"})
                return
            body = self.rfile.read(length) if length else b""
            status, payload = handle(channel, method, self.path, self.headers, body,
                                     self.client_address[0])
            self._send(status, payload)

        def _send(self, status: int, payload: dict) -> None:
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):        # noqa: N802 — BaseHTTPRequestHandler's naming
            self._answer("POST")

        def do_GET(self):         # noqa: N802
            self._answer("GET")

        def log_message(self, fmt, *args):
            # The default logs to stderr with the full request line. The token is a
            # header, not a query param, so nothing secret is in it — but a remote
            # control plane's request log belongs in the app's own log, not stderr.
            from core import caps
            caps.log("reverse channel: " + (fmt % args))

    return ThreadingHTTPServer((host, port), Handler)


# --- process control ------------------------------------------------------------------

def _file_logger():
    def log(message: str) -> None:
        try:
            config.ensure_dirs()
            with open(config.LOG_PATH, "a", encoding="utf-8") as handle:
                handle.write(f"{time.strftime('%H:%M:%S')}  {message}\n")
        except Exception:                       # noqa: BLE001 — a log must never kill it
            pass
    return log


def pidfile_path() -> str:
    return os.path.join(config.SUPPORT_DIR, PIDFILE)


def write_pidfile() -> str:
    config.ensure_dirs()
    path = pidfile_path()
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))
    os.chmod(path, 0o600)
    return path


def read_pid() -> int | None:
    try:
        with open(pidfile_path(), encoding="utf-8") as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return None


def kill(sleeper=time.sleep, signaller=os.kill) -> str:
    """Stop it. One command, and it does not come back on its own.

    SIGTERM, then SIGKILL if it is still there. A stale pidfile is cleaned up rather
    than reported as a failure — "nothing is running" is the desired end state either
    way.
    """
    pid = read_pid()
    if pid is None:
        return "reverse channel: no pidfile — nothing to kill"
    try:
        signaller(pid, signal.SIGTERM)
    except ProcessLookupError:
        _remove_pidfile()
        return f"reverse channel: pid {pid} was already gone (stale pidfile removed)"
    except PermissionError:
        return f"reverse channel: pid {pid} is not ours to kill"
    for _ in range(20):
        sleeper(0.1)
        try:
            signaller(pid, 0)
        except (ProcessLookupError, PermissionError):
            _remove_pidfile()
            return f"reverse channel: stopped (pid {pid})"
    try:
        signaller(pid, signal.SIGKILL)
    except OSError:
        pass
    _remove_pidfile()
    return f"reverse channel: killed (pid {pid} ignored SIGTERM)"


def _remove_pidfile() -> None:
    try:
        os.remove(pidfile_path())
    except OSError:
        pass


def status() -> str:
    pid = read_pid()
    state = "off" if not enabled() else "enabled"
    if pid is None:
        return f"reverse channel: {state}, not running"
    try:
        os.kill(pid, 0)
    except OSError:
        return f"reverse channel: {state}, pidfile says {pid} but it is not running"
    return f"reverse channel: {state}, running as pid {pid}, workspace {workspace_root()}"


def serve(host: str | None = None, port: int | None = None) -> None:
    """Bind the tailnet and serve. Refuses on anything it cannot vouch for."""
    if not enabled():
        raise Disabled("reverse_channel.enabled is false — turn it on in config first")
    secret = token()
    if not secret:
        raise Disabled("no reverse_channel_token in the Keychain — refusing to listen "
                       "without one")
    if len(secret) < MIN_TOKEN_CHARS:
        # There is no rate limit on this endpoint (see the module docstring), so the
        # token's length IS the brute-force budget. A short one is the likeliest real
        # failure, and it is the one thing that can be checked before opening the port.
        raise Disabled(f"reverse_channel_token is shorter than {MIN_TOKEN_CHARS} "
                       f"characters — that is the only thing standing between the "
                       f"tailnet and this Mac's shell")
    from core import caps
    caps.set_profile(REVERSE_PROFILE)
    # Standalone process: nothing has registered a log sink, so the channel's own
    # diagnostics would go nowhere. Point them at the app's log file.
    caps.set_log_sink(_file_logger())
    bind = resolve_bind_host(host or config.get("reverse_channel.bind"))
    listen_port = int(port or config.get("reverse_channel.port", 8791) or 8791)
    server = build_server(ReverseChannel(), bind, listen_port)
    write_pidfile()
    config.activity(f"🔌  reverse channel listening on {bind}:{listen_port} "
                    f"({workspace_root()})")
    # SIGTERM is what `--kill` sends first; shutting the socket down cleanly on it is
    # what makes "one command" a graceful stop rather than a half-served request.
    signal.signal(signal.SIGTERM, lambda *_a: threading.Thread(
        target=server.shutdown, daemon=True).start())
    try:
        server.serve_forever()
    finally:
        server.server_close()
        _remove_pidfile()
        config.activity("🔌  reverse channel stopped")


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Thrivbe Voice reverse channel")
    parser.add_argument("--kill", action="store_true", help="stop it")
    parser.add_argument("--status", action="store_true", help="is it up, and where")
    parser.add_argument("--host", default=None, help="tailnet address to bind")
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)

    if args.kill:
        print(kill())
        return 0
    if args.status:
        print(status())
        return 0
    try:
        serve(args.host, args.port)
    except (Disabled, Refused) as refusal:
        print(f"reverse channel refused to start: {refusal}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
