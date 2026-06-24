#!/usr/bin/env python3
"""Thrivbe Voice — a hands-free macOS menubar voice agent.

Lives in the menu bar (🎙). **Double-tap Control** to start/stop a live, hands-free
conversation: OpenAI Realtime speech-to-speech (in realtime.py), with barge-in, an
optional agentic shell, cross-session memory, and awareness of what's under your
cursor. One mode, one voice (OpenAI) — no push-to-talk, no ElevenLabs.
"""
import os, sys, subprocess, threading, time

# py2app puts the frozen python312.zip ahead of Contents/Resources on sys.path,
# so `import realtime/pill/config` would load STALE zipped copies. Put our own dir
# first so loose Resources/*.py win — this also lets us deploy edits with cp +
# relaunch (no rebuild, no TCC re-grant).
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
except Exception:
    pass

import rumps
import rumps.rumps as rumps_core
from pynput import keyboard

import config

# --- diagnostics: log to the app's own log dir (out of /tmp for the product)
LOG_PATH = config.LOG_PATH
def LOG(msg):
    try:
        config.ensure_dirs()
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass

def accessibility_trusted():
    """True if macOS grants this process the Accessibility right pynput needs."""
    try:
        from ApplicationServices import AXIsProcessTrusted
        return bool(AXIsProcessTrusted())
    except Exception as e:
        LOG(f"AX check failed: {e}")
        return None


def patch_rumps_status_item():
    """Make rumps 0.4 use the modern NSStatusBarButton API on current macOS."""

    def set_status_bar_title(self):
        title = self._app["_title"] or self._app["_name"] or ""
        button = self.nsstatusitem.button()
        if button is not None:
            button.setTitle_(title)
            return
        self.nsstatusitem.setTitle_(title)

    def set_status_bar_icon(self):
        image = self._app["_icon_nsimage"]
        button = self.nsstatusitem.button()
        if button is not None:
            button.setImage_(image)
            if image is None:
                button.setTitle_(self._app["_title"] or self._app["_name"] or "")
            return
        self.nsstatusitem.setImage_(image)
        self.fallbackOnName()

    rumps_core.NSApp.setStatusBarTitle = set_status_bar_title
    rumps_core.NSApp.setStatusBarIcon = set_status_bar_icon


patch_rumps_status_item()


def load_env():
    # Dev fallback only: in Robin's workspace this seeds keys from ~/Thrivbe-AI/.env
    # so config.secret()'s env fallback finds them. Shipped copies use the Keychain.
    path = os.path.join(os.path.expanduser("~/Thrivbe-AI"), ".env")
    for line in open(path, encoding="utf-8") if os.path.exists(path) else []:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
load_env()
# A Finder/launchd-started .app inherits a minimal PATH without Homebrew; restore it
# so anything the app shells out to (the agentic shell's `open`, etc.) resolves.
os.environ["PATH"] = "/opt/homebrew/bin:/opt/homebrew/sbin:" + os.environ.get("PATH", "")


# --- macOS context grab (used by live mode each turn) -----------------------
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

def _collect_text(el, out, depth=0):
    """Walk the element's subtree, gathering visible text (value/title/desc)."""
    if depth > 8 or len(out) >= 60:
        return
    for attr in ("AXSelectedText", "AXValue", "AXTitle", "AXDescription"):
        v = _ax(el, attr)
        if v is not None:
            s = str(v).strip()
            if len(s) >= 2 and not s.startswith("AX") and s not in out:
                out.append(s)
    kids = _ax(el, "AXChildren")
    if kids:
        for k in list(kids)[:25]:
            _collect_text(k, out, depth + 1)

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
        LOG(f"selection read failed: {e}")
    return ""

def grab_context():
    """Context for the agent: (1) text the user has MARKED/selected, and
    (2) the CONTENT under the mouse cursor. Both via AX — fast, no Cmd-C."""
    app = osa('tell application "System Events" to name of first process whose frontmost is true')
    parts = []

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
        LOG(f"cursor context failed: {e}")

    if parts:
        return f"App: {app}\n\n" + "\n\n".join(parts)
    return f"Frontmost app: {app} (no marked text or readable content under cursor)"


# --- menubar app ------------------------------------------------------------
class VoiceAgent(rumps.App):
    def __init__(self):
        super().__init__("🎙", quit_button=None)
        self.status = "idle"
        self._setup_checked = False
        # live conversation mode (Realtime API) — toggled from the hotkey thread,
        # reconciled onto the main thread in _tick (AppKit isn't thread-safe).
        self.live_on = False
        self.live = None
        self.pill = None
        self._pill_shown = False
        # double-tap Control detection
        self._ctrl_press_t = 0.0
        self._ctrl_clean = False     # True while a Control press has no other key with it
        self._last_ctrl_tap = 0.0
        self.shell_item = rumps.MenuItem("Agentic shell (runs commands)", callback=self.toggle_shell)
        self.shell_item.state = 1 if config.get("live.agentic_shell", False) else 0
        ins, outs = self._list_audio()
        self._mic_root, self._mic_items = self._device_submenu(
            "🎙 Microphone", config.get("audio.input_device"), ins, self._pick_mic)
        self._spk_root, self._spk_items = self._device_submenu(
            "🔊 Speaker", config.get("audio.output_device"), outs, self._pick_spk)
        self.menu = [
            rumps.MenuItem("🎧 Live conversation (double-tap Control)", callback=lambda _: self.toggle_live()),
            None,
            self.shell_item,
            self._mic_root,
            self._spk_root,
            None,
            rumps.MenuItem("Set OpenAI key…", callback=lambda _: self._set_key("openai", "OpenAI API key (sk-…)")),
            rumps.MenuItem("Run setup again…", callback=lambda _: self._onboard(force=True)),
            None,
            rumps.MenuItem("Quit", callback=rumps.quit_application),
        ]
        # reflect status into the menubar icon from the main thread
        rumps.Timer(self._tick, 0.3).start()
        self.hotkey_listener = keyboard.Listener(
            on_press=self._on_key_press,
            on_release=self._on_key_release,
        )
        self.hotkey_listener.start()
        trusted = accessibility_trusted()
        LOG(f"STARTED. accessibility_trusted={trusted}  log={LOG_PATH}")
        if trusted is False:
            LOG("NOT TRUSTED — grant 'Thrivbe Voice' in Accessibility + Input Monitoring, then relaunch.")
            rumps.notification("Thrivbe Voice", "Permission needed",
                               "Enable 'Thrivbe Voice' in Accessibility + Input Monitoring, then relaunch.")
        print("Ready: double-tap Control for a live conversation.")

    ICONS = {"idle": "🎙", "listening": "🔴", "thinking": "💭", "speaking": "🗣"}

    def _tick(self, _):
        if not self._setup_checked:        # first-run onboarding, once the app loop is live
            self._setup_checked = True
            self._onboard()
        self.title = self.ICONS.get(self.status, "🎙")
        self._reconcile_pill()

    def _reconcile_pill(self):
        """Show/hide/update the floating pill — main-thread only (called from _tick)."""
        pill_state = self.status if self.status in ("listening", "thinking", "speaking") else "listening"
        if self.live_on:
            if self.pill is None:
                try:
                    from pill import Pill
                    self.pill = Pill()
                except Exception as e:
                    LOG(f"pill init failed: {e!r}")
                    return
            if not self._pill_shown:
                self.pill.show(pill_state)
                self._pill_shown = True
            else:
                self.pill.set_state(pill_state)
        elif self.pill is not None and self._pill_shown:
            self.pill.hide()
            self._pill_shown = False

    def toggle_shell(self, item):
        on = not bool(item.state)
        item.state = 1 if on else 0
        config.set_("live.agentic_shell", on)
        rumps.notification("Thrivbe Voice", f"Agentic shell {'ON' if on else 'OFF'}",
                           "Restart live conversation to apply." if on else
                           "Live mode will only answer, not run commands.")

    # --- audio device pickers -----------------------------------------------
    def _list_audio(self):
        """(input names, output names) for the device-picker submenus."""
        try:
            import sounddevice as sd
            ins, outs = [], []
            for d in sd.query_devices():
                if d["max_input_channels"] > 0 and d["name"] not in ins:
                    ins.append(d["name"])
                if d["max_output_channels"] > 0 and d["name"] not in outs:
                    outs.append(d["name"])
            return ins, outs
        except Exception as e:
            LOG(f"audio list failed: {e!r}")
            return [], []

    def _device_submenu(self, title, current, names, cb):
        """Build a submenu of 'System default' + each device, checkmarking the
        current choice. Returns (root MenuItem, [child items])."""
        root = rumps.MenuItem(title)
        items = []
        default = rumps.MenuItem("System default", callback=cb)
        default.state = 0 if current else 1
        root.add(default); items.append(default)
        for n in names:
            it = rumps.MenuItem(n, callback=cb)
            it.state = 1 if (current and current == n) else 0
            root.add(it); items.append(it)
        return root, items

    def _check_group(self, items, sender):
        for it in items:
            it.state = 1 if it is sender else 0

    def _pick_mic(self, sender):
        name = None if sender.title == "System default" else sender.title
        config.set_("audio.input_device", name)
        self._check_group(self._mic_items, sender)
        rumps.notification("Thrivbe Voice", "Microphone set",
                           f"{sender.title} — applies on next live conversation")

    def _pick_spk(self, sender):
        name = None if sender.title == "System default" else sender.title
        config.set_("audio.output_device", name)
        self._check_group(self._spk_items, sender)
        rumps.notification("Thrivbe Voice", "Speaker set",
                           f"{sender.title} — applies on next live conversation")

    # --- onboarding / settings (product setup) ------------------------------
    def _set_key(self, name, prompt):
        """Prompt for an API key and store it in the Keychain (main-thread only)."""
        try:
            w = rumps.Window(message=prompt, title="Thrivbe Voice", default_text="",
                             ok="Save", cancel="Cancel", dimensions=(360, 24))
            r = w.run()
            if r.clicked and r.text.strip():
                ok = config.set_secret(name, r.text.strip())
                rumps.notification("Thrivbe Voice", "Saved" if ok else "Failed",
                                   f"{name} key {'stored in Keychain' if ok else 'could not be saved'}")
                return ok
        except Exception as e:
            LOG(f"set_key error: {e!r}")
        return False

    def _onboard(self, force=False):
        """First-run setup: get the OpenAI key, then deep-link the permission panes.
        Skipped silently once complete (and for Robin, since .env seeds the key)."""
        try:
            cfg = config.load()
            done = cfg.get("onboarding_complete") and config.has_required_keys()
            if done and not force:
                return
            if force or not config.secret("openai"):
                self._set_key("openai",
                              "Welcome! Paste your OpenAI API key (sk-…). Get one at platform.openai.com/api-keys")
            # deep-link the permission panes the app needs
            for pane in ("Privacy_ListenEvent", "Privacy_Accessibility", "Privacy_Microphone"):
                subprocess.run(["open", f"x-apple.systempreferences:com.apple.preference.security?{pane}"])
            rumps.notification("Thrivbe Voice", "Grant 3 permissions",
                               "Enable Thrivbe Voice in Input Monitoring, Accessibility & Microphone, then relaunch.")
            config.set_("onboarding_complete", True)
        except Exception as e:
            LOG(f"onboard error: {e!r}")

    # --- double-tap Control hotkey ------------------------------------------
    TAP_MAX = 0.25      # press shorter than this = a "tap"
    DOUBLE_GAP = 0.40   # two taps within this = double-tap

    def _is_ctrl(self, key):
        return key in (keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r)

    def _on_key_press(self, key):
        # NOTHING here may raise — a thrown callback kills the whole pynput
        # listener (no more hotkey at all). Hence the broad guard.
        try:
            if self._is_ctrl(key):
                self._ctrl_press_t = time.monotonic()
                self._ctrl_clean = True       # so far no other key with this Control
            else:
                # Any non-Control key cancels a pending double-tap and marks the
                # current Control press "dirty" (it's part of a shortcut, e.g. ⌃C).
                self._ctrl_clean = False
                self._last_ctrl_tap = 0.0
        except Exception as e:
            LOG(f"key press error: {e!r}")

    def _on_key_release(self, key):
        try:
            if not self._is_ctrl(key):
                return
            now = time.monotonic()
            held = now - self._ctrl_press_t
            # a clean, quick Control tap (no other key) — count toward double-tap
            if self._ctrl_clean and held < self.TAP_MAX:
                if now - self._last_ctrl_tap < self.DOUBLE_GAP:
                    self._last_ctrl_tap = 0.0
                    LOG("Control DOUBLE-TAP -> toggle live")
                    self.toggle_live()
                else:
                    self._last_ctrl_tap = now
        except Exception as e:
            LOG(f"key release error: {e!r}")

    def toggle_live(self):
        """Start/stop the Realtime live session. Called from the hotkey thread or
        menu — touches no AppKit (the pill is reconciled on the main thread in _tick)."""
        if self.live_on:
            LOG("LIVE: stopping")
            self.live_on = False
            if self.live:
                self.live.stop()
                self.live = None
            self.status = "idle"
            return
        LOG("LIVE: starting")
        try:
            import realtime
            self.live = realtime.LiveSession(on_state=self._on_live_state)
            self.live_on = True
            self.status = "listening"
            self.live.start()
        except Exception as e:
            LOG(f"LIVE start failed: {e!r}")
            self.live_on = False
            self.status = "idle"

    def _on_live_state(self, state):
        """Called from the realtime thread — only mutate plain attributes here."""
        self.status = state


if __name__ == "__main__":
    # realtime.py does `from agent import grab_context, LOG`. When this file runs as
    # __main__, alias it as `agent` so that import resolves to THIS already-initialised
    # module instead of re-importing (which would re-run load_env).
    sys.modules.setdefault("agent", sys.modules[__name__])
    VoiceAgent().run()
