"""Pam's hands: click, type_text, press_keys — the surface-independent half.

The mac half (`mac/hands.py`) does the clicking. This half decides whether a given
action has to be confirmed out loud first, and runs it through `caps.hands()`.
"""
from __future__ import annotations

import re

from core import caps

HAND_TOOLS = ("open_app", "click", "type_text", "press_keys")

# A click on one of these words is outward-facing or destructive, so it stages behind
# the spoken confirm gate like gmail_send. ponytail: word-list heuristic — an unlabeled
# icon that sends (a paper-plane with no AX text) slips through; upgrade path is asking
# Jev "is this click irreversible?" over the screenshot + element text.
RISKY_WORDS = re.compile(
    r"\b(send|delete|remove|trash|erase|pay|buy|purchase|place order|checkout|check out|"
    r"transfer|submit|publish|post|tweet|share|confirm|unsubscribe|"
    r"discard|overwrite|uninstall|empty trash|merge|deploy|slett|send inn|"
    r"betal|kjøp|publiser)\b", re.I)

# Chords that send or destroy in most Mac apps (Mail, Slack, Messages, Finder).
RISKY_KEYS = {"cmd+enter", "cmd+return", "cmd+shift+d", "cmd+delete", "cmd+backspace",
              "cmd+shift+delete", "cmd+shift+backspace", "cmd+option+delete"}


def normalize_keys(keys: str) -> str:
    """'Command + Return' -> 'cmd+return'. Order of modifiers is kept as given."""
    alias = {"command": "cmd", "⌘": "cmd", "control": "ctrl", "alt": "option",
             "opt": "option", "⌥": "option", "⇧": "shift", "return": "return",
             "esc": "escape", "del": "delete"}
    parts = [p.strip().lower() for p in re.split(r"\s*\+\s*", keys.strip()) if p.strip()]
    return "+".join(alias.get(p, p) for p in parts)


def needs_confirmation(name: str, args: dict) -> str:
    """'' if the action may run now, else the reason it must be confirmed first."""
    if name == "click":
        label = (args.get("label") or "").strip()
        if not label and args.get("x") is not None and args.get("y") is not None:
            label = caps.hands().element_at(float(args["x"]), float(args["y"]))
        if RISKY_WORDS.search(label):
            return f"click '{label}'"
    elif name == "press_keys":
        combo = normalize_keys(args.get("keys", ""))
        if combo in RISKY_KEYS:
            return f"press {combo}"
    return ""


def run(name: str, args: dict) -> str:
    h = caps.hands()
    if name == "click":
        x, y = args.get("x"), args.get("y")
        return h.click(label=(args.get("label") or None),
                       x=None if x is None else float(x),
                       y=None if y is None else float(y),
                       double=bool(args.get("double")))
    if name == "open_app":
        return h.open_app(args.get("target", ""))
    if name == "type_text":
        return h.type_text(args.get("text", ""))
    if name == "press_keys":
        return h.press_keys(normalize_keys(args.get("keys", "")))
    return f"unknown hand tool {name}"
