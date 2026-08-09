"""What each surface may DO — as DATA, in one place.

`core.caps` answers "how do I reach the machine" (log sink, screen, clipboard, audio
transport) and fails soft when a surface has no implementation. That is the right shape
for *context*: a missing screenshot degrades an answer.

It is the wrong shape for *tools*. A tool that exists in the schema but cannot work is
worse than absent: the model reads the description, offers it out loud, calls it, and
gets an error the user hears as a failure. And a drift checker comparing the two
surfaces sees an identical tool list while the two machines behave differently — which
is exactly the blindness that let the Mac and the phone bridge diverge for a year.

So each surface declares a PROFILE, and the profile is a literal below. Two consequences
follow mechanically:

  * `tools_for()` REMOVES a surface's excluded tools from the schema before it is sent.
    Absent, not present-and-erroring.
  * `check_tool_drift.py` reads `PROFILES` and treats a declared exclusion as an
    intended difference and anything else as drift. The difference is visible in the
    check's output instead of hidden in a handler.

`shell_gate` is the other half. Robin's ruling (2026-08-09): Thrivbe-1 gets FULL
`run_shell` on its own filesystem, but a DESTRUCTIVE command stages through the spoken
confirmation gate that `gmail_send` and `kernel_decide` already use. Reads run free.
What counts as destructive is `core.destructive` — also data, also reviewable.

The fallback profile is `unknown`, and it is deliberately the STRICTEST one: a surface
that forgets to register gets the staging gate, not the free shell. A registration bug
must cost a confirmation prompt, never an unstaged `rm -rf` on the server.
"""
from __future__ import annotations

# --- the tools that only exist where there is a Mac ----------------------------------
# Grouped by the machine fact that makes them impossible elsewhere, never as one flat
# list — the grouping is the review: you can check "is that still true?" per line.
# (Tuples, not set unions, because check_tool_drift.py evaluates this file without
# importing it and its literal evaluator understands `frozenset(a + b)`.)

# pbcopy + a synthetic Cmd-V into the frontmost window. There is no front window on a
# headless box, and `caps.clipboard()`'s null impl would answer "no clipboard here".
_CLIPBOARD_TOOLS = ("put_text",)

# herdr lanes: named panes in Robin's terminal workspace on THIS Mac. `~/.local/bin/herdr`
# is a Mac binary talking to a Mac tmux server; there is nothing for the server to drive.
# The server's hand-off path is `os_delegate` (the kernel worker), which both surfaces have.
_HERDR_LANE_TOOLS = ("delegate", "continue_task", "close_finished_tasks", "fleet")

MAC_ONLY_TOOLS = frozenset(_CLIPBOARD_TOOLS + _HERDR_LANE_TOOLS)

# Gate names for `shell_gate`. Free = run it, the surface is the user's own machine and
# he is sitting at it. Stage = a destructive command becomes a pending action that only
# his next short spoken affirmation executes.
SHELL_FREE = "free"
SHELL_STAGE_DESTRUCTIVE = "stage_destructive"

PROFILES = {
    "mac": {
        "excluded_tools": frozenset(),
        "shell_gate": SHELL_FREE,
        "shell_host": "this Mac",
        # `Shell` sources the rc file so Robin's PATH and the `claude` zsh function work.
        "shell_binaries": ("/bin/zsh",),
        "shell_rc": "~/.zshrc",
        "has_screen_context": True,
        "has_clipboard": True,
        "has_keychain": True,
    },
    "server": {
        "excluded_tools": MAC_ONLY_TOOLS,
        # Robin's ruling: full shell on Thrivbe-1's own filesystem, destructive staged.
        "shell_gate": SHELL_STAGE_DESTRUCTIVE,
        "shell_host": "Thrivbe-1",
        "shell_binaries": ("/bin/bash", "/bin/sh"),
        "shell_rc": "~/.bashrc",
        "has_screen_context": False,
        "has_clipboard": False,
        "has_keychain": False,
    },
    # Nobody registered. Strictest of everything: full tool list (a superset only ever
    # costs a fail-soft error) but the staging gate on the shell (a missing gate costs
    # the filesystem).
    "unknown": {
        "excluded_tools": frozenset(),
        "shell_gate": SHELL_STAGE_DESTRUCTIVE,
        "shell_host": "this machine",
        "shell_binaries": ("/bin/zsh", "/bin/bash", "/bin/sh"),
        "shell_rc": "~/.zshrc",
        "has_screen_context": False,
        "has_clipboard": False,
        "has_keychain": False,
    },
}

FALLBACK_PROFILE = "unknown"


class UnknownProfile(KeyError):
    """A surface named a profile that does not exist here — a typo, or a surface built
    against a newer core. Raised rather than defaulted, because silently falling back
    would hand a server the Mac's free shell."""


def get(name: str | None) -> dict:
    """The profile dict for `name`. None/absent -> the strict fallback."""
    if not name:
        return PROFILES[FALLBACK_PROFILE]
    try:
        return PROFILES[name]
    except KeyError:
        raise UnknownProfile(name) from None


def excluded_tools(name: str | None) -> frozenset:
    return frozenset(get(name)["excluded_tools"])


def shell_gate(name: str | None) -> str:
    return get(name)["shell_gate"]


def shell_host(name: str | None) -> str:
    return get(name)["shell_host"]


def tools_for(name: str | None, tools) -> list:
    """`tools` minus everything this surface's profile excludes.

    The one place a tool list is narrowed. Callers pass core's canonical TOOLS; what
    comes back is what the model is allowed to know exists.
    """
    gone = excluded_tools(name)
    return [t for t in tools if t.get("name") not in gone]


def surface_note(name: str | None, tools=None) -> str:
    """One prompt block telling the model which machine it is on and what is NOT here.

    Derived from the profile, never hand-written per surface: `core.live_prompt`'s
    LIVE_SYSTEM is shared by every surface (the drift checker requires that), so the
    surface-specific truth has to be assembled at runtime or it would be a lie on one
    of them. `tools` is only used to name the absent tools in the order core ships them.
    """
    profile = get(name)
    gone = frozenset(profile["excluded_tools"])
    lines = [f"THIS SURFACE: you are running on {profile['shell_host']}. "
             f"run_shell, and everything else you do, acts there — not on any other "
             f"machine Robin owns."]
    if not profile["has_clipboard"]:
        # The shared base prompt opens by saying this runs on the user's Mac, and it
        # cannot say otherwise without becoming a second prompt. So contradict it here,
        # explicitly, rather than leaving the model to pick between two claims.
        lines.append("Anything earlier in these instructions that describes you as "
                     "running on a Mac, or offers to paste into the window the user is "
                     "in, does not apply here — this machine has no desktop at all.")
    if not profile["has_screen_context"]:
        lines.append("There is no screen, no cursor context and no screenshot here: "
                     "never claim to see what he is looking at. Ask instead.")
    if gone:
        named = [t["name"] for t in (tools or []) if t.get("name") in gone] or sorted(gone)
        lines.append(
            "These tools do not exist on this surface and are not in your tool list: "
            + ", ".join(named)
            + ". Do not describe them, promise them, or apologise for them — hand work "
              "off with os_delegate instead.")
    if profile["shell_gate"] == SHELL_STAGE_DESTRUCTIVE:
        lines.append(
            "Reads run freely here. A command that could destroy or change state "
            "(delete, stop or restart a service, overwrite a file, install a package, "
            "kill a process, force-push) does NOT run when you call it: it is staged, "
            "and Robin's next spoken yes is what runs it. So say plainly what the "
            "command will do and ask him to confirm.")
    return "\n".join(lines)
