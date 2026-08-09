#!/usr/bin/env python3
"""Settings window — a WKWebView hosting settings.html, bridged to config.py.

Replaces the cluttered menubar dropdown with a real preferences window (the Aqua-
style layout). The menu now just has Live conversation / Settings… / Quit; everything
configurable lives here. JS ⇄ Python is a tiny promise-style RPC over a single
WKScriptMessageHandler named "bridge".

Only the window and the native bits live here. The values it renders — the voice list,
the HH:MM validator, the memory counters, the Tools inventory — are `core.settings`.
"""
import json
import os
import subprocess
import threading

from Cocoa import (NSObject, NSWindow, NSBackingStoreBuffered, NSMakeRect, NSApp,
                   NSWindowStyleMaskTitled, NSWindowStyleMaskClosable,
                   NSWindowStyleMaskResizable, NSWindowStyleMaskMiniaturizable)
from WebKit import WKWebView, WKWebViewConfiguration

from core import caps, config, paths
from core import settings as core_settings
from core.settings import VERSION, VOICES, _greeting, _hhmm, _mem_stats, _tools  # noqa: F401

# Security & Privacy panes the UI may deep-link to (the only valid openPane args).
_SECURITY_PANES = {"Privacy_Microphone", "Privacy_Accessibility", "Privacy_ListenEvent",
                   "Privacy_ScreenCapture"}

_window = None
_webview = None
_bridge = None
_agent = None


def _log(msg: str) -> None:
    caps.log(f"settings: {msg}")


# --- audio helpers ----------------------------------------------------------
def _devices():
    """(input names, output names) for the pickers — reuses the agent's scan."""
    try:
        return _agent._list_audio()
    except Exception:
        return [], []


def _pick_folder(want_file: bool = False, title: str = "Choose the base folder"):
    """Native chooser. want_file=False picks a directory (base folder); want_file=True
    picks a file (e.g. a memory DB). Returns the chosen POSIX path or None. Runs on the
    main thread (called from the WebKit message handler), as NSOpenPanel requires."""
    try:
        from AppKit import NSOpenPanel, NSApp
        NSApp.activateIgnoringOtherApps_(True)
        panel = NSOpenPanel.openPanel()
        panel.setCanChooseFiles_(bool(want_file))
        panel.setCanChooseDirectories_(not want_file)
        panel.setAllowsMultipleSelection_(False)
        panel.setTitle_(title)
        panel.setPrompt_("Choose")
        if panel.runModal() == 1 and panel.URLs():   # 1 == NSModalResponseOK
            return panel.URLs()[0].path()
    except Exception as e:
        _log(f"pick path failed: {e!r}")
    return None


def _device_index(name, want_input):
    """Resolve a device name (substring) to a sounddevice index; None = default."""
    if not name:
        return None
    try:
        import sounddevice as sd
        key = "max_input_channels" if want_input else "max_output_channels"
        for i, d in enumerate(sd.query_devices()):
            if d[key] > 0 and name in d["name"]:
                return i
    except Exception as e:
        _log(f"device index failed: {e!r}")
    return None


def _test_audio(which):
    """Speaker: play a short tone. Mic: record 1.5s and play it back. Runs off-thread."""
    def work():
        try:
            import numpy as np, sounddevice as sd
            sr = 24000
            out = _device_index(config.get("audio.output_device"), want_input=False)
            if which == "spk":
                t = np.linspace(0, 0.4, int(sr * 0.4), endpoint=False)
                tone = (0.3 * np.sin(2 * np.pi * 440 * t)).astype("float32")
                sd.play(tone, sr, device=out); sd.wait()
            else:  # mic: record then play back
                mic = _device_index(config.get("audio.input_device"), want_input=True)
                _push_toast("Listening… speak now")
                rec = sd.rec(int(sr * 1.5), samplerate=sr, channels=1,
                             dtype="float32", device=mic); sd.wait()
                _push_toast("Playing it back")
                sd.play(rec, sr, device=out); sd.wait()
        except Exception as e:
            _log(f"audio test failed: {e!r}")
            _push_toast("Audio test failed — check the device")
    threading.Thread(target=work, daemon=True).start()


# --- login item (Open at login) --------------------------------------------
def _app_path():
    """Path to the installed .app bundle, if there is one to launch at login."""
    p = os.path.join(paths.PROJECT_ROOT, "Thrivbe Voice.app")
    return p if os.path.exists(p) else None


def _set_login_item(on: bool):
    app = _app_path()
    if not app:
        _push_toast("Open at login works once the app is installed")
        return
    name = "Thrivbe Voice"
    try:
        if on:
            subprocess.run(["osascript", "-e",
                f'tell application "System Events" to make login item at end '
                f'with properties {{path:"{app}", hidden:true, name:"{name}"}}'],
                capture_output=True, text=True)
        else:
            subprocess.run(["osascript", "-e",
                f'tell application "System Events" to delete login item "{name}"'],
                capture_output=True, text=True)
    except Exception as e:
        _log(f"login item failed: {e!r}")


# --- state ------------------------------------------------------------------
def _state() -> dict:
    ins, outs = _devices()
    return core_settings.state(ins, outs, getattr(_agent, "live_on", False))


def _eval(js: str):
    if _webview is not None:
        try:
            _webview.evaluateJavaScript_completionHandler_(js, None)
        except Exception as e:
            _log(f"eval failed: {e!r}")


def _push_state():
    _eval(f"window.__push({json.dumps(_state())})")


def _push_toast(msg: str):
    _eval(f"window.__toast({json.dumps(msg)})")


# --- RPC dispatch -----------------------------------------------------------
def _handle(fn, arg):
    if fn == "ready":
        _push_state(); return None
    if fn == "set":
        key, value = arg.get("key"), arg.get("value")
        if key in ("live.quiet_hours.start", "live.quiet_hours.end"):
            # <input type=time> can post "" when cleared — never write a value
            # _in_quiet_hours can't parse, or the window silently falls back.
            value = _hhmm(value)
            if value is None:
                _push_toast("Enter a time as HH:MM"); return None
        config.set_(key, value)
        if key == "system.open_at_login":
            _set_login_item(bool(value))
        if key == "live.agentic_shell":
            _push_state()          # the Tools panel's off/on badges depend on it
        return None
    if fn == "setKey":
        ok = config.set_secret("openai", str(arg).strip())
        _push_toast("Key saved" if ok else "Could not save key")
        return _state()
    if fn == "setGeminiKey":
        ok = config.set_secret("gemini", str(arg).strip())
        _push_toast("Key saved" if ok else "Could not save key")
        return _state()
    if fn == "refresh":
        _push_state(); return None
    if fn == "pickFolder":
        path = _pick_folder()
        if path:
            config.set_("live.workspace", path)
            _push_state()
        return path
    if fn == "pickMemDb":
        path = _pick_folder(want_file=True, title="Choose the memory database")
        if path:
            config.set_("live.memory.claude_mem_db", path)
            _push_state()
        return path
    if fn == "test":
        _test_audio(arg); return None
    if fn == "toggleLive":
        try:
            _agent.toggle_live()
        except Exception as e:
            _log(f"toggleLive failed: {e!r}")
        _push_state(); return None
    if fn == "openSetup":
        try:
            _agent._onboard(force=True)
        except Exception as e:
            _log(f"openSetup failed: {e!r}")
        return None
    if fn == "openPane":
        # arg arrives from the JS bridge (untrusted): only open known Security panes,
        # never let it smuggle a second URL/argument into `open`.
        if arg in _SECURITY_PANES:
            subprocess.run(["open",
                f"x-apple.systempreferences:com.apple.preference.security?{arg}"])
        else:
            _log(f"openPane rejected unknown anchor: {arg!r}")
        return None
    return None


class Bridge(NSObject):
    def userContentController_didReceiveScriptMessage_(self, ucc, message):
        try:
            body = message.body()
            fn = body.get("fn"); arg = body.get("arg"); req = body.get("reqId")
            result = _handle(fn, arg)
            if req is not None:
                _eval(f"window.__resolve({int(req)}, {json.dumps(result)})")
        except Exception as e:
            _log(f"bridge error: {e!r}")


def open_settings(agent):
    """Create (or re-show) the settings window. Main-thread only — called from a menu
    callback, which AppKit already runs on the main thread."""
    global _window, _webview, _bridge, _agent
    _agent = agent
    if _window is not None:
        _push_state()
        _window.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)
        return
    try:
        # In a py2app bundle settings.py lives inside lib/python3.12.zip, so
        # dirname(__file__) is not a real directory. core.paths.RESOURCE_DIR resolves
        # to Contents/Resources in a bundle (where data_files ship settings.html) and
        # to the repo root in dev.
        html_path = os.path.join(paths.RESOURCE_DIR, "settings.html")
        html = open(html_path, encoding="utf-8").read()

        cfg = WKWebViewConfiguration.alloc().init()
        _bridge = Bridge.alloc().init()
        cfg.userContentController().addScriptMessageHandler_name_(_bridge, "bridge")

        rect = NSMakeRect(0, 0, 920, 720)
        _webview = WKWebView.alloc().initWithFrame_configuration_(rect, cfg)
        _webview.loadHTMLString_baseURL_(html, None)

        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                 | NSWindowStyleMaskResizable | NSWindowStyleMaskMiniaturizable)
        _window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, NSBackingStoreBuffered, False)
        _window.setTitle_("Thrivbe Voice")
        # ponytail: NSWindow defaults isReleasedWhenClosed=YES, which frees the ObjC
        # object on the red close button while the Python global _window still points
        # at it — reopen then segfaults. False = close just hides; reopen re-shows it.
        _window.setReleasedWhenClosed_(False)
        _window.setContentView_(_webview)
        _window.center()
        _window.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)
    except Exception as e:
        _log(f"open_settings failed: {e!r}")
