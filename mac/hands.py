"""Hands capability: click, type and press keys in the front window of this Mac.

Click by LABEL first — an Accessibility search of the front window for a control whose
text matches, then AXPress on it. That is exact and does not depend on the model's aim.
Click by x/y (0-1000 over the screenshot's window) is the fallback for things AX cannot
name. Typing posts Unicode key events, so the clipboard is left alone.

Needs the Accessibility grant the app already has for screen context. Refuses outright
inside the same sensitive apps the screenshot refuses to capture (password managers,
banking) — Pam may not see them, so she may not touch them either.
"""
from __future__ import annotations

import time

from core import caps
from mac import macos_context as ctx

_TEXT_ATTRS = ("AXTitle", "AXDescription", "AXValue", "AXHelp", "AXPlaceholderValue")
_PRESSABLE = {"AXButton", "AXLink", "AXMenuItem", "AXMenuBarItem", "AXCheckBox",
              "AXRadioButton", "AXPopUpButton", "AXMenuButton", "AXTab", "AXCell",
              "AXRow", "AXDisclosureTriangle", "AXTextField", "AXTextArea",
              "AXComboBox", "AXSearchField", "AXImage", "AXStaticText"}
_MAX_NODES = 1500   # ponytail: BFS cap; a huge web page may hide deep controls past it

# US ANSI virtual key codes. Named keys + letters + digits is all a voice ask needs.
_KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
    "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19,
    "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28,
    "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "l": 37, "j": 38,
    "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46, ".": 47,
    "`": 50, "return": 36, "enter": 36, "tab": 48, "space": 49, "delete": 51,
    "backspace": 51, "escape": 53, "forwarddelete": 117, "home": 115, "end": 119,
    "pageup": 116, "pagedown": 121, "left": 123, "right": 124, "down": 125, "up": 126,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100,
    "f9": 101, "f10": 109, "f11": 103, "f12": 111,
}


def _flags():
    import Quartz
    return {"cmd": Quartz.kCGEventFlagMaskCommand, "shift": Quartz.kCGEventFlagMaskShift,
            "option": Quartz.kCGEventFlagMaskAlternate, "ctrl": Quartz.kCGEventFlagMaskControl,
            "fn": Quartz.kCGEventFlagMaskSecondaryFn}


def _front_pid_app() -> tuple[int, str]:
    from AppKit import NSWorkspace
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    return (app.processIdentifier(), app.localizedName() or "") if app else (-1, "")


def _blocked() -> str:
    """A refusal sentence if the front window is one Pam must not touch, else ''."""
    app, title = ctx._frontmost_app_and_title()
    if not app and not title:
        return "I can't tell which app is in front, so I won't touch it."
    if ctx.screenshot_denied(app, title):
        return f"{app} is a sensitive app (passwords or banking) — I don't touch it. You do this one."
    return ""


def _text(el) -> str:
    parts = []
    for a in _TEXT_ATTRS:
        v = ctx._ax(el, a)
        if isinstance(v, str) and v.strip():
            parts.append(v.strip())
    return " ".join(parts)


def _geometry(el):
    """(x, y, w, h) in screen points, or None."""
    from ApplicationServices import (AXValueGetValue, kAXValueCGPointType,
                                     kAXValueCGSizeType)
    pos, size = ctx._ax(el, "AXPosition"), ctx._ax(el, "AXSize")
    if pos is None or size is None:
        return None
    ok1, p = AXValueGetValue(pos, kAXValueCGPointType, None)
    ok2, s = AXValueGetValue(size, kAXValueCGSizeType, None)
    return (p.x, p.y, s.width, s.height) if ok1 and ok2 else None


def _find(label: str):
    """Best AX element in the front app's focused window whose text matches `label`:
    exact text beats a prefix beats a substring; a pressable role beats plain text."""
    from ApplicationServices import AXUIElementCreateApplication
    pid, _ = _front_pid_app()
    if pid < 0:
        return None
    root = AXUIElementCreateApplication(pid)
    start = [w for w in (ctx._ax(root, "AXFocusedWindow"), ctx._ax(root, "AXMenuBar")) if w]
    want = label.strip().lower()
    best, best_rank = None, 99
    queue, seen = list(start), 0
    while queue and seen < _MAX_NODES:
        el = queue.pop(0)
        seen += 1
        role = ctx._ax(el, "AXRole") or ""
        text = _text(el).lower()
        if text and role in _PRESSABLE:
            rank = 0 if text == want else 1 if text.startswith(want) else 2 if want in text else 99
            if rank < 99 and role == "AXStaticText":
                rank += 3            # prefer the button over the words next to it
            if rank < best_rank:
                best, best_rank = el, rank
                if rank == 0:
                    break
        queue.extend(ctx._ax(el, "AXChildren") or [])
    return best


def _window_rect():
    """The rect the screenshot showed: same function, so x/y line up with what Pam saw."""
    region = ctx._focused_window_region()
    if region:
        x, y, w, h = (float(v) for v in region.split(","))
        return x, y, w, h
    import Quartz
    b = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
    return b.origin.x, b.origin.y, b.size.width, b.size.height


def _to_screen(x: float, y: float):
    rx, ry, rw, rh = _window_rect()
    x, y = min(max(x, 0.0), 1000.0), min(max(y, 0.0), 1000.0)
    return rx + x / 1000.0 * rw, ry + y / 1000.0 * rh


def _mouse_click(sx: float, sy: float, double: bool = False) -> None:
    import Quartz
    pt = (sx, sy)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, Quartz.CGEventCreateMouseEvent(
        None, Quartz.kCGEventMouseMoved, pt, Quartz.kCGMouseButtonLeft))
    for n in ((1, 2) if double else (1,)):
        for kind in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
            ev = Quartz.CGEventCreateMouseEvent(None, kind, pt, Quartz.kCGMouseButtonLeft)
            Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGMouseEventClickState, n)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        time.sleep(0.03)


class MacHands(caps.Hands):
    def click(self, label=None, x=None, y=None, double=False) -> str:
        if (why := _blocked()):
            return why
        if label:
            el = _find(label)
            if el is not None:
                shown = _text(el)[:60] or label
                role = ctx._ax(el, "AXRole") or ""
                if not double and role not in ("AXStaticText", "AXTextField", "AXTextArea",
                                                "AXSearchField", "AXComboBox", "AXImage"):
                    from ApplicationServices import AXUIElementPerformAction
                    if AXUIElementPerformAction(el, "AXPress") == 0:
                        return f"pressed '{shown}'"
                geo = _geometry(el)
                if geo:
                    gx, gy, gw, gh = geo
                    _mouse_click(gx + gw / 2, gy + gh / 2, double)
                    return f"clicked '{shown}'"
            if x is None or y is None:
                return (f"found nothing labelled '{label}' in the front window — look at "
                        "the screenshot and click by x/y instead, or scroll first")
        if x is None or y is None:
            return "click needs a label or both x and y"
        sx, sy = _to_screen(x, y)
        _mouse_click(sx, sy, double)
        hit = self.element_at(x, y)
        return f"clicked at {int(x)},{int(y)}" + (f" on '{hit}'" if hit else "")

    def element_at(self, x, y) -> str:
        try:
            from ApplicationServices import (AXUIElementCopyElementAtPosition,
                                             AXUIElementCreateSystemWide)
            sx, sy = _to_screen(x, y)
            err, el = AXUIElementCopyElementAtPosition(AXUIElementCreateSystemWide(),
                                                       sx, sy, None)
            return _text(el)[:80] if err == 0 and el is not None else ""
        except Exception:
            return ""

    def open_app(self, target: str) -> str:
        import subprocess
        target = target.strip()
        if not target:
            return "open_app needs an app name or a web address"
        # Only an app name (`open -a`, which can only launch apps) or an http(s) URL.
        # Never a path: `open x.command` RUNS it (see confirm_gate's open_file note).
        if target.lower().startswith(("http://", "https://")):
            argv = ["open", target]
        elif "/" in target or target.startswith("."):
            return "open_app takes an app name or an http(s) address, not a file path"
        else:
            argv = ["open", "-a", target]
        r = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return f"could not open {target}: {(r.stderr or '').strip()[:120]}"
        time.sleep(0.8)              # let it come to the front before the next screenshot
        return f"opened {target}"

    def type_text(self, text: str) -> str:
        if (why := _blocked()):
            return why
        import Quartz
        for i in range(0, len(text), 16):
            chunk = text[i:i + 16]
            units = len(chunk.encode("utf-16-le")) // 2
            for down in (True, False):
                ev = Quartz.CGEventCreateKeyboardEvent(None, 0, down)
                Quartz.CGEventKeyboardSetUnicodeString(ev, units, chunk)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
            time.sleep(0.01)
        return f"typed {len(text)} characters"

    def press_keys(self, keys: str) -> str:
        if (why := _blocked()):
            return why
        import Quartz
        parts = [p for p in keys.split("+") if p]
        if not parts:
            return "no keys given"
        flags, mods = 0, _flags()
        for m in parts[:-1]:
            if m not in mods:
                return f"unknown modifier '{m}' — use cmd, shift, option, ctrl"
            flags |= mods[m]
        key = parts[-1]
        code = _KEYCODES.get(key)
        if code is None:
            return f"unknown key '{key}'"
        for down in (True, False):
            ev = Quartz.CGEventCreateKeyboardEvent(None, code, down)
            Quartz.CGEventSetFlags(ev, flags)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        return f"pressed {keys}"


def install() -> None:
    caps.set_hands(MacHands())
