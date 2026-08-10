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

THERE IS ONE BRAIN, AND IT IS THIS MAC (Robin's ruling, 2026-08-10). The phone is a
remote microphone and speaker for the process running on his Mac, not a second agent on
another machine. So a profile no longer says "which computer am I" — every profile here
acts on the same Mac. It says WHICH SEAT the human is in, because that is what actually
changes: at the desk he can see the screen and take the keyboard back; on the phone he
can do neither.

That is also why there is no shell gate in this file any more, and no
`core.destructive` next to it. The gating question ("this command is dangerous — stage
it and wait for a spoken yes") only ever existed because a surface Robin was NOT at had
a full shell on a machine he could not watch. The desk surface is Robin at his own
keyboard, so its shell runs free, exactly as it always has. The phone surface has NO
`run_shell` at all — not gated, not staged, ABSENT from the schema and refused by the
dispatcher. Three rounds of adversarial review broke every attempt to let reads through
a shell allowlist (`env` as an exec wrapper, `git ls-remote --upload-pack`, `uniq`'s
second operand writing a file); an allowlist of shell commands is not a thing this
codebase is going to get right, so the phone does not get a shell to allowlist.

The spoken confirmation gate itself STAYS — `gmail_send` and `kernel_decide` still
stage and still wait for a plain yes (`core.confirm_gate`). What went away is the idea
that a shell command is something a surface can be trusted to classify.

The fallback profile is `unknown`, and it is deliberately the STRICTEST one: a surface
that forgets to register gets the phone's exclusions, so a registration bug costs a
missing tool rather than an unwatched `rm -rf`.
"""
from __future__ import annotations

import re

# --- the tools the phone does not get ------------------------------------------------
# Grouped by the fact that makes each one wrong when Robin is holding a phone, never as
# one flat list — the grouping is the review: you can check "is that still true?" per
# line.
# (Tuples, not set unions, because check_tool_drift.py evaluates this file without
# importing it and its literal evaluator understands `frozenset(a + b)`.)

# The shell. Not gated, not staged — absent. See the module docstring: the phone is the
# surface Robin cannot watch, and a shell he cannot watch is the one thing that has to
# be impossible rather than merely careful. Filesystem work from the phone goes to a
# delegate lane or `os_delegate`, which run ON this Mac and report back out loud.
_SHELL_TOOLS = ("run_shell",)

# Desk context: `put_text` is pbcopy plus a synthetic Cmd-V into the FRONTMOST window.
# The brain is on the Mac, so the paste would genuinely land — into whatever window
# happens to be in front on a screen Robin is not looking at, which is worse than a
# failure, because it succeeds silently in the wrong place.
_DESK_CONTEXT_TOOLS = ("put_text",)

# NOT excluded, deliberately, and this is the line that changed on 2026-08-10: the herdr
# lanes (`delegate`, `continue_task`, `close_finished_tasks`, `fleet`) drive named panes
# in Robin's terminal workspace on THIS Mac, through a Mac binary talking to a Mac tmux
# server. Under the old plan the brain lived on Thrivbe-1 and could not reach any of
# that, so they were excluded. The brain is now the Mac process itself, so the lanes are
# local calls and they work from the phone exactly as they do at the desk. Robin
# delegating from the sofa is the whole point of the phone surface.

PHONE_EXCLUDED_TOOLS = frozenset(_SHELL_TOOLS + _DESK_CONTEXT_TOOLS)

# --- what a REMAINING tool's description may still promise ---------------------------
# Removing `run_shell` from the schema is not the whole job. A tool DESCRIPTION is prompt
# text — the model reads it before it decides — so a tool that survives the cut and ends
# with "keep using run_shell to read deeper files" (which `focus` did, shipped, until
# 2026-08-10) makes the exact promise the exclusion exists to prevent, one level down.
#
# `tools_for` therefore rewrites descriptions as well as dropping tools, and the rewrite
# is MECHANICAL rather than a table of known-bad sentences: any sentence naming a tool
# this surface does not have is dropped, so a description edited next year cannot
# reintroduce the promise without the phone-schema test failing.
#
# What IS a table is the sentence that goes in its place. The dropped sentence usually
# answered "and then what?", and an unanswered question is where a model starts
# inventing — so a tool whose guidance is surface-dependent names its shell-less
# alternative here, once, next to the exclusions that trigger it.
ABSENT_TOOL_GUIDANCE = {
    "focus": ("Once focused, what it loaded IS what you have on this surface: use "
              "semsearch_query or hybrid_rag_search for what is not in it, and hand "
              "anything that has to be read off disk to a delegate lane or os_delegate."),
}

# --- how explicit the spoken "yes" has to be, per staged tool ------------------------
# The gate is one mechanism, but not every staged action costs the same to get wrong.
# A mis-sent email is embarrassing and recoverable — Robin can send a correction. A
# command executed on his behalf is not always recoverable. So the affirmation bar is
# DATA here, next to the profiles, rather than a constant buried in `core.live_session`:
#
#   normal — a short leading affirmation, possibly with a courtesy word ("yes please",
#            "confirmed", "go ahead").
#   strict — an unhedged leading yes / do it / kjør, at most three words, and drawn from
#            a NARROWER vocabulary: the weak-and-ambiguous affirmations ("ok", "confirm",
#            "proceed", "approved") do not carry an irreversible action.
#
# Both bars reject questions, hedges and continuations; `core.confirm_gate` owns that
# machinery. This table only decides which bar a given staged tool has to clear.
AFFIRM_NORMAL = "normal"
AFFIRM_STRICT = "strict"

# `run_shell` is still named here even though no LIVE conversation stages one any more:
# `mac/reverse_channel.py` — committed, off by default, kept for the later Thrivbe-1
# project — stages every command it is handed, and it reads this table. A tool that can
# be staged anywhere needs its bar written down here, or it silently gets the weak one.
CONFIRM_STRICTNESS = {
    "run_shell": AFFIRM_STRICT,
}

# Anything not named above gets the normal bar — which is still an explicit spoken
# affirmation, just a slightly wider vocabulary. New IRREVERSIBLE tools belong in the
# table above; add the line in the same commit that adds the tool.
DEFAULT_CONFIRM_STRICTNESS = AFFIRM_NORMAL

PROFILES = {
    # Robin at his own keyboard. Unchanged, on purpose: it works like it works now.
    "mac": {
        "excluded_tools": frozenset(),
        "host": "this Mac",
        # `Shell` sources the rc file so Robin's PATH and the `claude` zsh function work.
        "shell_binaries": ("/bin/zsh",),
        "shell_rc": "~/.zshrc",
        "has_screen_context": True,
        "has_clipboard": True,
        "has_keychain": True,
    },
    # The phone PWA: a microphone and a speaker for the session running on this Mac,
    # reached over the tailnet. SAME machine, same kernel tools, same accounts, same
    # delegate lanes — the difference is the seat, not the computer. Robin is not at the
    # keyboard, so there is no screen to read, no window to paste into, and no shell.
    #
    # `shell_binaries` / `shell_rc` are still the Mac's, and that is not a leftover: the
    # session's persistent shell object is the same Mac zsh either way (`focus` moves
    # its working directory). What the phone does not get is a TOOL that reaches it.
    "phone": {
        "excluded_tools": PHONE_EXCLUDED_TOOLS,
        "host": "Robin's Mac",
        "shell_binaries": ("/bin/zsh",),
        "shell_rc": "~/.zshrc",
        "has_screen_context": False,
        "has_clipboard": False,
        "has_keychain": True,
    },
    # The reverse channel (mac/reverse_channel.py): an HTTP surface on this Mac that a
    # Thrivbe-1-hosted brain could call back into. It is OFF by default and nothing in
    # the current architecture turns it on — the brain is already here — but the code
    # stays committed for that later project, so its profile stays with it. It is not a
    # conversational surface: it has no tool schema of its own (`excluded_tools` is
    # therefore empty and unused) and every operation it offers stages behind the
    # confirm gate on its own account.
    "mac_reverse": {
        "excluded_tools": frozenset(),
        "host": "Robin's Mac",
        "shell_binaries": ("/bin/zsh",),
        "shell_rc": "~/.zshrc",
        "has_screen_context": True,
        "has_clipboard": True,
        "has_keychain": True,
    },
    # Nobody registered. Strictest of everything: the phone's exclusions, so a forgotten
    # `install()` costs a missing tool rather than a shell on a surface nobody classified.
    "unknown": {
        "excluded_tools": PHONE_EXCLUDED_TOOLS,
        "host": "this machine",
        "shell_binaries": ("/bin/zsh", "/bin/bash", "/bin/sh"),
        "shell_rc": "~/.zshrc",
        "has_screen_context": False,
        "has_clipboard": False,
        "has_keychain": False,
    },
}

FALLBACK_PROFILE = "unknown"

# The tool whose absence defines a shell-less surface. Named once, here, so `has_shell`
# and the prompt text cannot disagree about what "has a shell" means.
SHELL_TOOL = "run_shell"


class UnknownProfile(KeyError):
    """A surface named a profile that does not exist here — a typo, or a surface built
    against a newer core. Raised rather than defaulted, because silently falling back
    would hand a surface Robin cannot watch the desk profile."""


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


def host(name: str | None) -> str:
    """The machine this surface's actions land on. One brain, so this is the Mac for
    every real surface — it is still profile data because the sentence Robin hears
    before he confirms an action names it."""
    return get(name)["host"]


def has_shell(name: str | None) -> bool:
    """Whether `run_shell` exists on this surface at all.

    Derived from the exclusions rather than stored beside them: two fields that can
    disagree is how a prompt ends up promising a tool the schema does not carry.
    """
    return SHELL_TOOL not in excluded_tools(name)


def confirm_strictness(tool: str | None) -> str:
    """Which affirmation bar a staged `tool` has to clear. Surface-independent: the
    cost of a wrong action does not depend on who is asking."""
    return CONFIRM_STRICTNESS.get(tool or "", DEFAULT_CONFIRM_STRICTNESS)


_SENTENCES = re.compile(r"(?<=[.!?])\s+")


def _names_absent_tool(sentence: str, gone: frozenset) -> bool:
    return any(re.search(rf"\b{re.escape(tool)}\b", sentence) for tool in gone)


def _keep_sentences(text: str, gone: frozenset) -> str:
    """`text` without any sentence that names a tool this surface does not have.

    Returns the original object untouched when nothing is dropped — the desk surface
    excludes nothing, and its schema has to stay byte-identical to what core declares.
    """
    parts = _SENTENCES.split(text)
    kept = [part for part in parts if not _names_absent_tool(part, gone)]
    if len(kept) == len(parts):
        return text
    return " ".join(part.strip() for part in kept if part.strip())


def _without_absent_tools(tool: dict, gone: frozenset) -> dict | None:
    """`tool` with every description scrubbed of the tools this surface lacks.

    Returns the SAME dict when nothing changed, a NEW one when it did (never a mutation
    of core's canonical TOOLS — every surface reads that list), and None when the tool's
    own description was entirely about something absent, which makes the tool itself a
    promise this surface cannot keep.
    """
    changed = False

    def walk(node, top):
        nonlocal changed
        if isinstance(node, dict):
            out = {}
            for key, value in node.items():
                if key == "description" and isinstance(value, str):
                    kept = _keep_sentences(value, gone)
                    if kept != value:
                        changed = True
                        guidance = ABSENT_TOOL_GUIDANCE.get(tool.get("name")) if top else None
                        if guidance:
                            kept = (kept + " " + guidance).strip()
                    out[key] = kept
                else:
                    out[key] = walk(value, False)
            return out
        if isinstance(node, list):
            return [walk(item, False) for item in node]
        return node

    rewritten = walk(tool, True)
    if not changed:
        return tool
    if not (rewritten.get("description") or "").strip():
        return None
    return rewritten


def tools_for(name: str | None, tools) -> list:
    """`tools` minus everything this surface's profile excludes — including the mentions.

    The one place a tool list is narrowed. Callers pass core's canonical TOOLS; what
    comes back is what the model is allowed to know exists. Two things are removed:
    the excluded tools themselves, and any sentence in a SURVIVING tool's description
    that names one of them (see ABSENT_TOOL_GUIDANCE). A surface that excludes nothing
    gets core's list back object for object, unchanged.
    """
    gone = excluded_tools(name)
    if not gone:
        return list(tools)
    out = []
    for tool in tools:
        if tool.get("name") in gone:
            continue
        scrubbed = _without_absent_tools(tool, gone)
        if scrubbed is not None:
            out.append(scrubbed)
    return out


def surface_note(name: str | None, tools=None) -> str:
    """One prompt block telling the model which seat the user is in and what is NOT here.

    Derived from the profile, never hand-written per surface: `core.live_prompt`'s
    LIVE_SYSTEM is shared by every surface (the drift checker requires that), so the
    surface-specific truth has to be assembled at runtime or it would be a lie on one
    of them. `tools` is only used to name the absent tools in the order core ships them.
    """
    profile = get(name)
    gone = frozenset(profile["excluded_tools"])
    lines = []
    if has_shell(name):
        lines.append(f"THIS SURFACE: you are running on {profile['host']}, and Robin is "
                     f"sitting at it. run_shell, and everything else you do, acts there "
                     f"— not on any other machine he owns.")
    else:
        lines.append(
            f"THIS SURFACE: you are the assistant on {profile['host']}, and Robin is "
            f"reaching you from his phone — the phone is only a microphone and a "
            f"speaker. Everything you do still happens on {profile['host']}: his files, "
            f"his accounts, his delegate lanes, his kernel. What is different is that "
            f"he is NOT at that keyboard and cannot see that screen.")
        lines.append(
            "You have NO shell here. There is no run_shell in your tool list, so "
            "anything earlier in these instructions that describes a persistent shell, "
            "tells you to read a file or look something up on disk with it, or offers "
            "to run a command does not apply on this surface. Do not describe a shell, "
            "promise one, or apologise for not having one — when work needs the "
            "filesystem or a command, hand it to a delegate lane (delegate, or "
            "continue_task for work already running) or to os_delegate. Those run on "
            "that Mac for you and report back out loud.")
    if not profile["has_clipboard"]:
        # The shared base prompt offers to paste into "the window the user is in". On
        # this surface the window is on a Mac he is not looking at, so a successful
        # paste is worse than a failed one — it lands somewhere he cannot see.
        lines.append("Anything earlier in these instructions that offers to type or "
                     "paste into the window the user is in does not apply here: he is "
                     "not in front of that window, so text put there would land where "
                     "he cannot see it. Read it out loud instead, or send it somewhere "
                     "he is actually looking.")
    if not profile["has_screen_context"]:
        lines.append("No screen context, cursor context or screenshot reaches you here: "
                     "never claim to see what he is looking at. Ask instead.")
    if gone:
        named = [t["name"] for t in (tools or []) if t.get("name") in gone] or sorted(gone)
        lines.append(
            "These tools do not exist on this surface and are not in your tool list: "
            + ", ".join(named)
            + ". Do not describe them, promise them, or apologise for them — hand work "
              "off with delegate or os_delegate instead.")
    return "\n".join(lines)
