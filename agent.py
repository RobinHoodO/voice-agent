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
        self._term_win_id = None      # Terminal window id of the Activity feed, if open
        # double-tap Control detection
        self._ctrl_press_t = 0.0
        self._ctrl_clean = False     # True while a Control press has no other key with it
        self._last_ctrl_tap = 0.0
        # Slim menu — everything configurable now lives in the Settings window.
        self.menu = [
            rumps.MenuItem("🎧 Live conversation (double-tap Control)", callback=lambda _: self.toggle_live()),
            rumps.MenuItem("⚙ Settings…", callback=lambda _: self._open_settings()),
            None,
            rumps.MenuItem("Quit", callback=rumps.quit_application),
        ]
        # Auto-wake: detached delegate jobs drop a .done sentinel in config.TASKS_DIR
        # when they finish. Seed _announced with any that already exist so we don't
        # replay stale results on launch, then poll for new ones.
        self._announced = set()
        try:
            config.ensure_dirs()
            for f in os.listdir(config.TASKS_DIR):
                if f.endswith(".done"):
                    self._announced.add(f[:-5])
        except Exception as e:
            LOG(f"task seed failed: {e!r}")
        # reflect status into the menubar icon from the main thread
        rumps.Timer(self._tick, 0.3).start()
        rumps.Timer(self._check_tasks, 2.0).start()
        rumps.Timer(self._pump_pill_level, 0.05).start()   # feed mic level into the wave
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

    ICONS = {"idle": "🎙", "listening": "🔴", "thinking": "💭", "acting": "⚙️", "speaking": "🗣"}

    def _open_settings(self):
        try:
            import settings
            settings.open_settings(self)
        except Exception as e:
            LOG(f"open settings failed: {e!r}")

    def _tick(self, _):
        if not self._setup_checked:        # first-run onboarding, once the app loop is live
            self._setup_checked = True
            self._onboard()
        self.title = self.ICONS.get(self.status, "🎙")
        self._reconcile_pill()

    def _pump_pill_level(self, _):
        """Feed the live conversation's mic level into the wave pill (main-thread).
        Cheap no-op when not in a live conversation or the pill isn't shown."""
        live = getattr(self, "live", None)
        if not (self.live_on and live is not None and self.pill is not None and self._pill_shown):
            return
        # precise colour: apply the state the instant it changes (≤50ms), not on the 0.3s tick
        st = self.status if self.status in ("listening", "thinking", "acting", "speaking") else "listening"
        if st != getattr(self, "_pill_state_last", None):
            self._pill_state_last = st
            try:
                self.pill.set_state(st)
            except Exception:
                pass
        # Only cross the ObjC→JS bridge when the level actually changed — at 20Hz an
        # unconditional set_level (e.g. during silence) needlessly competes with the
        # AppKit/audio main loop. The shader smooths between updates.
        try:
            lvl = float(getattr(live, "level", 0.0))
            if abs(lvl - getattr(self, "_pill_level_last", -1.0)) >= 0.01:
                self._pill_level_last = lvl
                self.pill.set_level(lvl)
        except Exception:
            pass

    def _reconcile_pill(self):
        """Show/hide/update the floating pill — main-thread only (called from _tick)."""
        pill_state = self.status if self.status in ("listening", "thinking", "acting", "speaking") else "listening"
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
            # else: state changes are applied by _pump_pill_level (50ms, change-gated),
            # so no redundant per-tick set_state JS hop here.
        elif self.pill is not None and self._pill_shown:
            self.pill.hide()
            self._pill_shown = False
            self._pill_state_last = None

    def _open_activity_window(self):
        """Open a Terminal window tailing the curated Activity feed (not the raw log).
        Remembers the window id so the conversation can close it again."""
        if self._term_win_id:
            return
        try:
            config.activity("— watching —")   # guarantees the file exists for tail
            path = config.ACTIVITY_PATH.replace('"', '\\"')
            script = (
                'tell application "Terminal"\n'
                ' activate\n'
                f' do script "clear; tail -n 50 -f \\"{path}\\""\n'
                ' return id of window 1\n'
                'end tell')
            r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
            self._term_win_id = r.stdout.strip() or None
        except Exception as e:
            LOG(f"activity window failed: {e!r}")

    def _close_activity_window(self):
        """Close the Activity window when the conversation ends (if we opened one)."""
        wid, self._term_win_id = self._term_win_id, None
        if not wid:
            return
        try:
            subprocess.run(["osascript", "-e",
                f'tell application "Terminal" to close (every window whose id is {wid})'],
                capture_output=True, text=True)
        except Exception as e:
            LOG(f"activity window close failed: {e!r}")

    # --- auto-wake on background task completion -----------------------------
    def _check_tasks(self, _):
        """Main-thread poll: when a delegated job drops a .done sentinel, wake the
        agent (if idle) to speak its result. Skips jobs finished while we're already
        live — the user is mid-conversation, so we don't barge in (the .out file
        stays on disk; ponytail: idle-only auto-wake, no queueing)."""
        try:
            for f in sorted(os.listdir(config.TASKS_DIR)):
                if not f.endswith(".done"):
                    continue
                tid = f[:-5]
                if tid in self._announced:
                    continue
                self._announced.add(tid)
                if self.live_on:
                    LOG(f"task {tid} done while live — not auto-waking")
                    continue
                LOG(f"task {tid} done -> waking to speak result")
                self._wake_and_speak(self._read_task_out(tid))
        except FileNotFoundError:
            pass
        except Exception as e:
            LOG(f"task check failed: {e!r}")

    def _read_task_out(self, tid):
        """Read a finished job's captured output, stripped of terminal escape codes."""
        try:
            with open(os.path.join(config.TASKS_DIR, f"{tid}.out"),
                      encoding="utf-8", errors="replace") as fh:
                import re
                txt = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", fh.read()).strip()
        except Exception:
            txt = ""
        return txt or "(the task finished but produced no output)"

    def _wake_and_speak(self, text):
        """Open a live session that speaks `text` first, then stays listening so the
        user can follow up and close it manually. Mirrors toggle_live's start path."""
        if config.get("ui.show_terminal", False):
            config.reset_activity()
            self._open_activity_window()
        try:
            import realtime
            self.live = realtime.LiveSession(on_state=self._on_live_state, announce=text)
            self.live_on = True
            self.status = "listening"
            self.live.start()
        except Exception as e:
            LOG(f"wake-and-speak failed: {e!r}")
            self.live_on = False
            self.status = "idle"

    # --- audio device list (consumed by the Settings window) ----------------
    def _list_audio(self):
        """(input names, output names) for the device pickers."""
        try:
            import sounddevice as sd
            # PortAudio caches the device list at init, so hot-plugged gear (a
            # reconnected headset, AirPods, etc.) never appears until we re-init.
            # Refresh it first so the pickers always mirror the live system.
            # ponytail: global re-init would kill an active stream — skip mid-call;
            #           drop the guard once devices are scanned only when idle.
            if not self.live_on:
                try:
                    sd._terminate(); sd._initialize()
                except Exception as e:
                    LOG(f"audio reinit skipped: {e!r}")
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
            self._close_activity_window()   # detached background jobs keep running
            return
        LOG("LIVE: starting")
        if config.get("ui.show_terminal", False):
            config.reset_activity()         # fresh feed for this conversation
            self._open_activity_window()
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
