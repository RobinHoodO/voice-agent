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

import config


def _log(msg: str) -> None:
    try:
        from agent import LOG
        LOG(f"macos_context: {msg}")
    except Exception:
        pass


def osa(script):
    try:
        return subprocess.run(["osascript", "-e", script],
                              capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return ""


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


def grab_context():
    """Text context for the agent, each part independently toggleable in Settings:
      • read_cursor_context  → MARKED selection + CONTENT under the mouse cursor
      • read_window_context  → the FULL focused window's text (larger context)
    Both via AX — fast, no Cmd-C. (The screenshot/vision part is separate, see
    grab_window_screenshot.)"""
    app = osa('tell application "System Events" to name of first process whose frontmost is true')
    parts = []

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
            from ApplicationServices import AXUIElementCreateSystemWide
            fapp = _ax(AXUIElementCreateSystemWide(), "AXFocusedApplication")
            win = _ax(fapp, "AXFocusedWindow") if fapp is not None else None
            if win is not None:
                title = _ax(win, "AXTitle") or ""
                wt = []
                _collect_text(win, wt, max_depth=12, max_items=200, child_cap=40)
                blob = "\n".join(wt)[:6000]
                if blob.strip():
                    label = f"{app}" + (f" — {title}" if title else "")
                    parts.append(f"The full window I'm in ({label}) — larger context:\n{blob}")
        except Exception as e:
            _log(f"window context failed: {e}")

    if parts:
        return f"App: {app}\n\n" + "\n\n".join(parts)
    return f"Frontmost app: {app} (no readable text context)"


def _focused_window_region():
    """'x,y,w,h' of the frontmost app's largest on-screen window (for screencapture -R),
    or '' to fall back to the full screen. Uses CGWindowList so no AXValue geometry math."""
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
    import base64 as _b64
    f = os.path.join(tempfile.gettempdir(), "tv_window_ctx.jpg")
    region = _focused_window_region()
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
