"""macOS screen/context capture — text under the cursor, focused-window text (AX),
and a downscaled focused-window screenshot.

Extracted from agent.py so realtime.py can import it directly instead of reaching
back into the menubar module (that was the agent↔realtime coupling). Pure macOS I/O
here; no rumps/UI. Logging goes through a lazy shim so importing this never pulls in
agent at import time.
"""
import os
import subprocess
import tempfile

from core import caps, config


# Robin can extend this denylist as additional sensitive apps emerge.
SCREENSHOT_DENY_APPS = frozenset((
    "bank", "nordea", "dnb", "revolut", "paypal", "stripe", "coinbase",
    "helsenorge", "health", "patient", "journal",
    "1password", "bitwarden", "keychain access", "lastpass", "dashlane",
    "messages", "signal", "whatsapp", "telegram", "beeper", "mail", "gmail",
))
SCREENSHOT_DENY_TITLE = frozenset((
    "bank", "nordea", "dnb", "revolut", "paypal", "stripe", "coinbase",
    "helsenorge", "health", "patient", "journal",
    "1password", "bitwarden", "keychain access", "lastpass", "dashlane",
    "messages", "signal", "whatsapp", "telegram", "beeper", "mail", "gmail",
    "password", "login", "secure",
))


def screenshot_denied(app: str, title: str, cursor_title: str = "") -> bool:
    """True when the denylist above forbids capturing this app/window.

    The POLICY is unchanged and still lives entirely in the two frozensets above —
    this is the same expression `grab_window_screenshot` already evaluated inline,
    given a name so the reverse channel can ask the question before it captures and
    answer "refused: sensitive app" instead of the bare "" that means both "denied"
    and "no Screen Recording permission". One predicate, two callers; a term added to
    a denylist still lands in both by construction.
    """
    app_lower, title_lower = (app or "").lower(), (title or "").lower()
    cursor_title_lower = str(cursor_title or "").lower()
    return (any(term in app_lower for term in SCREENSHOT_DENY_APPS)
            or any(term in title_lower for term in SCREENSHOT_DENY_TITLE)
            or any(term in cursor_title_lower for term in SCREENSHOT_DENY_APPS)
            or any(term in cursor_title_lower for term in SCREENSHOT_DENY_TITLE))


def _log(msg: str) -> None:
    caps.log(f"macos_context: {msg}")


def osa(script):
    try:
        return subprocess.run(["osascript", "-e", script],
                              capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return ""


def _frontmost_app_and_title():
    """Return the frontmost app and its window title, or empty strings on error."""
    result = osa('''tell application "System Events"
        set frontmostProcess to first process whose frontmost is true
        set appName to name of frontmostProcess
        try
            set windowTitle to name of front window of frontmostProcess
        on error
            set windowTitle to ""
        end try
        return appName & "|||" & windowTitle
    end tell''')
    app, separator, title = result.partition("|||")
    return (app.strip(), title.strip()) if separator else ("", "")


def _ax(el, attr):
    """Read one AX attribute (attr is a plain string like 'AXValue')."""
    try:
        from ApplicationServices import AXUIElementCopyAttributeValue
        err, val = AXUIElementCopyAttributeValue(el, attr, None)
        return val if err == 0 else None
    except Exception:
        return None


def _collect_text(el, out, depth=0, max_depth=8, max_items=60, child_cap=25):
    """Walk the element's subtree, gathering visible text (value/title/desc).
    Budgets are bounded so even a huge window (Chrome DOM) stays fast; callers pass
    larger budgets for whole-window capture, defaults keep cursor capture cheap."""
    if depth > max_depth or len(out) >= max_items:
        return
    for attr in ("AXSelectedText", "AXValue", "AXTitle", "AXDescription"):
        v = _ax(el, attr)
        if v is not None:
            s = str(v).strip()
            if len(s) >= 2 and not s.startswith("AX") and s not in out:
                out.append(s)
    kids = _ax(el, "AXChildren")
    if kids:
        for k in list(kids)[:child_cap]:
            _collect_text(k, out, depth + 1, max_depth, max_items, child_cap)


_BROWSER_URL_SCRIPT = {
    "Safari": 'tell application "Safari" to get URL of current tab of front window',
    "Google Chrome": 'tell application "Google Chrome" to get URL of active tab of front window',
    "Brave Browser": 'tell application "Brave Browser" to get URL of active tab of front window',
    "Microsoft Edge": 'tell application "Microsoft Edge" to get URL of active tab of front window',
    "Arc": 'tell application "Arc" to get URL of active tab of front window',
}


def _browser_url(app: str) -> str:
    """Active tab URL if `app` is a known browser, else '' (best-effort, silent)."""
    script = _BROWSER_URL_SCRIPT.get(app)
    return osa(script) if script else ""


def _selected_text():
    """Whatever the user has highlighted, via the focused element (fast, no Cmd-C)."""
    try:
        from ApplicationServices import AXUIElementCreateSystemWide
        focused = _ax(AXUIElementCreateSystemWide(), "AXFocusedUIElement")
        if focused is not None:
            sel = _ax(focused, "AXSelectedText")
            if sel:
                return str(sel).strip()
    except Exception as e:
        _log(f"selection read failed: {e}")
    return ""


def _cursor_loc():
    """Current mouse location in global (top-left origin) coordinates."""
    from Quartz import CGEventCreate, CGEventGetLocation
    loc = CGEventGetLocation(CGEventCreate(None))
    return loc.x, loc.y


def _ax_window_under_cursor():
    """The AX window element directly under the mouse cursor (NOT the focused window —
    on a multi-monitor setup the user looks at whatever their cursor is over, which can be
    a different display/app than the frontmost one). Climbs from the hit element to its
    enclosing AXWindow. Returns (window_element_or_None, title)."""
    try:
        from ApplicationServices import (
            AXUIElementCreateSystemWide, AXUIElementCopyElementAtPosition)
        x, y = _cursor_loc()
        err, el = AXUIElementCopyElementAtPosition(
            AXUIElementCreateSystemWide(), x, y, None)
        if err != 0 or el is None:
            return None, ""
        cur = el
        for _ in range(12):
            if _ax(cur, "AXRole") == "AXWindow":
                return cur, (_ax(cur, "AXTitle") or "")
            parent = _ax(cur, "AXParent")
            if parent is None:
                break
            cur = parent
        win = _ax(el, "AXWindow")
        return win, (_ax(win, "AXTitle") or "" if win is not None else "")
    except Exception as e:
        _log(f"window-under-cursor failed: {e}")
        return None, ""


def _window_under_cursor_bounds():
    """'x,y,w,h' of the topmost normal window under the cursor (CGWindowList is front-to-back),
    or '' if none. Display-agnostic — the cursor's window wins regardless of which app is frontmost."""
    try:
        import Quartz
        x, y = _cursor_loc()
        wins = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID) or []
        for w in wins:
            if w.get("kCGWindowLayer", 0) != 0:
                continue
            b = w.get("kCGWindowBounds") or {}
            bx, by = b.get("X", 0) or 0, b.get("Y", 0) or 0
            bw, bh = b.get("Width", 0) or 0, b.get("Height", 0) or 0
            if bx <= x < bx + bw and by <= y < by + bh:
                return f"{int(bx)},{int(by)},{int(bw)},{int(bh)}"
    except Exception as e:
        _log(f"window-under-cursor bounds failed: {e}")
    return ""


def grab_context():
    """Text context for the agent, each part independently toggleable in Settings:
      • read_cursor_context  → MARKED selection + CONTENT under the mouse cursor
      • read_window_context  → the FULL focused window's text (larger context)
    Both via AX — fast, no Cmd-C. (The screenshot/vision part is separate, see
    grab_window_screenshot.)"""
    app = osa('tell application "System Events" to name of first process whose frontmost is true')
    parts = []

    if config.get("privacy.read_cursor_context", True) or config.get("privacy.read_window_context", False):
        url = _browser_url(app)
        if url:
            parts.append(f"Page URL: {url}")

    if config.get("privacy.read_cursor_context", True):
        selected = _selected_text()
        if selected:
            parts.append(f"The user has MARKED this text (prioritise it):\n{selected[:3500]}")
        try:
            from Quartz import CGEventCreate, CGEventGetLocation
            from ApplicationServices import (
                AXUIElementCreateSystemWide, AXUIElementCopyElementAtPosition)
            loc = CGEventGetLocation(CGEventCreate(None))
            err, el = AXUIElementCopyElementAtPosition(
                AXUIElementCreateSystemWide(), loc.x, loc.y, None)
            if err == 0 and el is not None:
                texts = []
                _collect_text(el, texts)
                blob = "\n".join(texts)[:3500]
                if blob.strip():
                    parts.append(f"Content under the mouse cursor:\n{blob}")
        except Exception as e:
            _log(f"cursor context failed: {e}")

    if config.get("privacy.read_window_context", False):
        try:
            # The window under the CURSOR, not the frontmost window — on multi-monitor the
            # user is looking at whatever their cursor is over, possibly another display/app.
            win, title = _ax_window_under_cursor()
            if win is not None:
                wt = []
                _collect_text(win, wt, max_depth=12, max_items=200, child_cap=40)
                blob = "\n".join(wt)[:6000]
                if blob.strip():
                    label = title or "window under cursor"
                    parts.append(f"The full window under my cursor ({label}) — larger context:\n{blob}")
        except Exception as e:
            _log(f"window context failed: {e}")

    if parts:
        return f"App: {app}\n\n" + "\n\n".join(parts)
    return f"Frontmost app: {app} (no readable text context)"


def _focused_window_region():
    """'x,y,w,h' for screencapture -R: the window UNDER THE CURSOR if there is one (so the
    screenshot follows the cursor across displays), else the frontmost app's largest
    on-screen window, else '' (full screen). Uses CGWindowList so no AXValue geometry math."""
    cursor_region = _window_under_cursor_bounds()
    if cursor_region:
        return cursor_region
    try:
        from AppKit import NSWorkspace
        import Quartz
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        pid = app.processIdentifier() if app else -1
        wins = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID) or []
        best, best_area = None, 0
        for w in wins:
            if w.get("kCGWindowOwnerPID") != pid or w.get("kCGWindowLayer", 0) != 0:
                continue
            b = w.get("kCGWindowBounds") or {}
            area = (b.get("Width", 0) or 0) * (b.get("Height", 0) or 0)
            if area > best_area:
                best_area, best = area, b
        if best:
            return f"{int(best['X'])},{int(best['Y'])},{int(best['Width'])},{int(best['Height'])}"
    except Exception as e:
        _log(f"window region failed: {e}")
    return ""


def grab_window_screenshot(max_dim=900, quality=45):
    """The focused window as a small JPEG (base64, no data-URI header), or '' on failure.
    Fast by design: captures only the focused window region (not the whole screen) and
    downscales hard — small payload = lower vision latency + cost. Needs macOS Screen
    Recording permission; without it the capture is empty and we return ''."""
    app, title = _frontmost_app_and_title()
    if not app and not title:
        _log("screenshot skipped: could not identify frontmost app (fail-closed)")
        return ""

    try:
        _, cursor_title = _ax_window_under_cursor()
    except Exception:
        cursor_title = ""

    if screenshot_denied(app, title, cursor_title):
        _log("screenshot skipped: sensitive app/window")
        return ""

    import base64 as _b64
    f = os.path.join(tempfile.gettempdir(), "tv_window_ctx.jpg")
    region = _focused_window_region()
    if not region and not app:
        _log("screenshot skipped: full-screen capture with unidentified app (fail-closed)")
        return ""
    cap = ["screencapture", "-x", "-o", "-t", "jpg"]
    if region:
        cap += ["-R", region]
    cap.append(f)
    try:
        subprocess.run(cap, capture_output=True, timeout=4)
        # Hard downscale + recompress for speed and token cost (best-effort).
        subprocess.run(["sips", "-Z", str(max_dim), "-s", "formatOptions", str(quality), f],
                       capture_output=True, timeout=4)
        with open(f, "rb") as fh:
            data = fh.read()
        return _b64.b64encode(data).decode() if data else ""
    except Exception as e:
        _log(f"window screenshot failed: {e}")
        return ""
    finally:
        try:
            os.remove(f)
        except Exception:
            pass
