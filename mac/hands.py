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
        if isinstance(v, str) and v.strip() and v.strip() not in parts:
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


def _web_area(win):
    """The page content of a browser window (AXWebArea), or None for non-browsers."""
    queue, n = [win], 0
    while queue and n < 400:
        el = queue.pop(0)
        n += 1
        if ctx._ax(el, "AXRole") == "AXWebArea":
            return el
        queue.extend(ctx._ax(el, "AXChildren") or [])
    return None


def _roots():
    """Where to search, page content first: [web area?, focused window, menu bar]."""
    from ApplicationServices import AXUIElementCreateApplication
    pid, app = _front_pid_app()
    if pid < 0:
        return [], app
    root = AXUIElementCreateApplication(pid)
    win = ctx._ax(root, "AXFocusedWindow")
    web = _web_area(win) if win is not None else None
    return [r for r in (web, win, ctx._ax(root, "AXMenuBar")) if r is not None], app


_FIELDS = ("AXTextField", "AXTextArea", "AXSearchField", "AXComboBox")


def _find(label: str, roles=None):
    """Best AX element in the front app's focused window whose text matches `label`:
    exact text beats a prefix beats a substring; a pressable role beats plain text.
    Page content is searched before the browser's own toolbar."""
    start, _ = _roots()
    roles = roles or _PRESSABLE
    want = label.strip().lower()
    best, best_rank = None, 99
    queue, seen = list(start), 0
    while queue and seen < _MAX_NODES:
        el = queue.pop(0)
        seen += 1
        role = ctx._ax(el, "AXRole") or ""
        text = _text(el).lower()
        if text and role in roles:
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


    def fill_field(self, label: str, text: str, submit: bool = False) -> str:
        if (why := _blocked()):
            return why
        el = _find(label, roles=_FIELDS)
        if el is None:
            return (f"no field labelled '{label}' — try read_controls, or click the field "
                    "and use type_text")
        from ApplicationServices import AXUIElementSetAttributeValue
        AXUIElementSetAttributeValue(el, "AXFocused", True)
        geo = _geometry(el)
        if geo:                       # a real click too: web fields often ignore AXFocused
            gx, gy, gw, gh = geo
            _mouse_click(gx + gw / 2, gy + gh / 2)
        time.sleep(0.15)
        self.press_keys("cmd+a")      # replace what was there, like a person would
        self.type_text(text)
        if submit:
            self.press_keys("return")
        return f"filled '{_text(el)[:50] or label}'" + (" and pressed return" if submit else "")

    def scroll(self, direction: str, amount: int) -> str:
        if (why := _blocked()):
            return why
        import Quartz
        amount = max(1, min(int(amount), 60))
        dy = {"up": amount, "down": -amount}.get(direction, 0)
        dx = {"left": amount, "right": -amount}.get(direction, 0)
        if not (dx or dy):
            return f"unknown direction '{direction}'"
        # Scroll events land under the pointer, so park it in the window's middle first.
        cx, cy = _to_screen(500, 500)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, Quartz.CGEventCreateMouseEvent(
            None, Quartz.kCGEventMouseMoved, (cx, cy), Quartz.kCGMouseButtonLeft))
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, Quartz.CGEventCreateScrollWheelEvent(
            None, Quartz.kCGScrollEventUnitLine, 2, dy, dx))
        return f"scrolled {direction} {amount}"

    def read_controls(self, filter: str = "") -> str:
        if (why := _blocked()):
            return why
        roots, app = _roots()
        if not roots:
            return "no window in front"
        want, lines, seen, queue = filter.strip().lower(), [], set(), roots[:1]
        n = 0
        while queue and n < _MAX_NODES and len(lines) < 80:
            el = queue.pop(0)
            n += 1
            role = ctx._ax(el, "AXRole") or ""
            text = _text(el)[:70]
            if text and role in _PRESSABLE and role != "AXStaticText":
                line = f"{role[2:].lower()}: {text}"
                if line not in seen and (not want or want in line.lower()):
                    seen.add(line)
                    lines.append(line)
            queue.extend(ctx._ax(el, "AXChildren") or [])
        return f"{app} — {len(lines)} controls:\n" + "\n".join(lines) if lines else \
            f"no labelled controls found in {app}; use the screenshot and click by x/y"

    def browser_tabs(self, action: str, match: str = "") -> str:
        _, front = _front_pid_app()
        browser = "Safari" if front == "Safari" else "Google Chrome"
        title = "name" if browser == "Safari" else "title"
        # One line per tab: "<window>\t<tab>\t<title>\t<url>". Matching happens here in
        # Python, not in AppleScript, so what gets switched is exactly what was listed.
        raw = ctx.osa(
            f'set sep to character id 9\n'   # `tab` means a browser tab inside the tell
            f'tell application "{browser}"\nif not running then return ""\nset out to ""\n'
            f'set wi to 0\nrepeat with w in windows\nset wi to wi + 1\nset ti to 0\n'
            f'repeat with t in tabs of w\nset ti to ti + 1\n'
            f'set out to out & wi & sep & ti & sep & ({title} of t) & sep & (URL of t) & linefeed\n'
            f'end repeat\nend repeat\nreturn out\nend tell')
        tabs = [r.split("\t", 3) for r in (raw or "").splitlines() if r.count("\t") >= 3]
        if not tabs:
            return f"{browser}: no open tabs (or not running, or no Automation permission)"
        if action == "list":
            return f"{browser}, {len(tabs)} tabs:\n" + "\n".join(
                f"{t[2][:70]} | {t[3][:90]}" for t in tabs)
        if action not in ("switch", "close"):
            return f"unknown action '{action}'"
        want = match.strip().lower()
        if not want:
            return "browser_tabs switch/close needs match"
        hit = next((t for t in tabs if want in t[2].lower()), None) or \
            next((t for t in tabs if want in t[3].lower()), None)
        if hit is None:
            return f"{browser}: no tab matches '{match}'"
        w, i = int(hit[0]), int(hit[1])
        if action == "close":
            cmd = f"close tab {i} of window {w}"
        elif browser == "Safari":
            cmd = f"set current tab of window {w} to tab {i} of window {w}\nset index of window {w} to 1\nactivate"
        else:
            cmd = f"set active tab index of window {w} to {i}\nset index of window {w} to 1\nactivate"
        ctx.osa(f'tell application "{browser}"\n{cmd}\nend tell')
        done = "closed" if action == "close" else "switched to"
        return f"{browser}: {done} '{hit[2][:70]}'"


def install() -> None:
    caps.set_hands(MacHands())
