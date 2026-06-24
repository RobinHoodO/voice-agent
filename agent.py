#!/usr/bin/env python3
"""Thrivbe Voice Agent — a standalone macOS menubar app you talk to.

Lives in the top menu bar (🎙). Hold Right-Option to record, release to get a
spoken answer. Click the icon → "Start talking" is kept as a fallback. It reads
whatever app/selection you're pointing at, can run
terminal commands in the Thrivbe workspace, and holds a conversation.

Brain: DeepSeek V3 via OpenRouter.  Ears: OpenAI STT.  Voice: ElevenLabs (→ say fallback).
"""
import os, sys, subprocess, tempfile, json, threading, time, re

# py2app puts the frozen python312.zip ahead of Contents/Resources on sys.path,
# so `import realtime/pill` would load STALE zipped copies. Put our own dir first
# so loose Resources/*.py win — this also lets us deploy edits with cp + relaunch
# (no rebuild, no TCC re-grant).
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
except Exception:
    pass

import rumps
import rumps.rumps as rumps_core
from openai import OpenAI
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

# --- config -----------------------------------------------------------------
# Push-to-talk's shell cwd + context root. Per-user: config live.workspace, else $HOME.
WORKSPACE = os.path.expanduser(config.get("live.workspace") or "~")

def detect_mic():
    """avfoundation device indices drift when audio gear connects/disconnects.
    Find the MacBook mic by name (fallback: first audio device)."""
    out = subprocess.run(["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
                         capture_output=True, text=True).stderr
    devices, in_audio = [], False
    for line in out.splitlines():
        if "audio devices" in line:
            in_audio = True; continue
        if "video devices" in line:
            in_audio = False; continue
        m = re.search(r"\]\s*\[(\d+)\]\s+(.+)$", line)
        if in_audio and m:
            devices.append((m.group(1), m.group(2).strip()))
    for idx, name in devices:
        if "MacBook" in name and "Microphone" in name:
            return f":{idx}"
    return f":{devices[0][0]}" if devices else ":0"

MIC = detect_mic()                           # e.g. ":0" — MacBook mic, auto-detected
STT_MODEL = "gpt-4o-mini-transcribe"
LLM_MODEL = config.get("ptt.brain_model") or "openai/gpt-4o-mini"   # push-to-talk brain (OpenRouter)
ELEVEN_VOICE = config.get("ptt.eleven_voice") or "21m00Tcm4TlvDq8ikWAM"

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
# A Finder/launchd-started .app inherits a minimal PATH without Homebrew, so
# ffmpeg/ffplay/say would not resolve. Restore it for push-to-talk + TTS.
os.environ["PATH"] = "/opt/homebrew/bin:/opt/homebrew/sbin:" + os.environ.get("PATH", "")

# Keys from Keychain (config), env fallback in dev. Empty key won't crash at
# construction — only on use — so a not-yet-onboarded user still launches cleanly.
openai_client = OpenAI(api_key=config.secret("openai") or "")
router = OpenAI(api_key=config.secret("openrouter") or config.secret("openai") or "",
                base_url="https://openrouter.ai/api/v1")

SYSTEM = f"""You are Robin's voice assistant, anchored in his Thrivbe workspace at {WORKSPACE}.
You answer OUT LOUD, so keep replies SHORT and conversational — 1-3 sentences, no markdown, no lists, no emoji.
You receive the UI element directly under Robin's MOUSE CURSOR (role, title, value, selected text). That is what he is pointing at — answer about THAT.
Answer directly and fast. Only use run_shell when Robin explicitly asks you to read a file, search, or do something on disk — never to "explore" on your own. If asked to do something, do it, then say briefly what you did."""

TOOLS = [{"type": "function", "function": {
    "name": "run_shell",
    "description": "Run a shell command in the Thrivbe workspace. Read files Robin points at, search, or act.",
    "parameters": {"type": "object",
        "properties": {"command": {"type": "string"}}, "required": ["command"]}}}]


# --- macOS context grab -----------------------------------------------------
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
    """Whatever Robin has highlighted, via the focused element (fast, no Cmd-C)."""
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
    """Context for the brain: (1) text Robin has MARKED/selected, and
    (2) the CONTENT under the mouse cursor. Both via AX — fast, no Cmd-C."""
    app = osa('tell application "System Events" to name of first process whose frontmost is true')
    parts = []

    selected = _selected_text()
    if selected:
        parts.append(f"Robin has MARKED this text (prioritise it):\n{selected[:3500]}")

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


# --- audio ------------------------------------------------------------------
class Recorder:
    def __init__(self):
        self.proc = None
        self.wav = None

    def start(self):
        self.wav = tempfile.mktemp(suffix=".wav")
        self.proc = subprocess.Popen(
            ["ffmpeg", "-y", "-f", "avfoundation", "-i", MIC, "-ac", "1", "-ar", "16000", self.wav],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop(self):
        if not self.proc:
            return None
        try:
            self.proc.communicate(input=b"q", timeout=5)
        except Exception:
            self.proc.terminate()
        self.proc = None
        return self.wav

def transcribe(wav):
    if not wav or not os.path.exists(wav) or os.path.getsize(wav) < 2000:
        return ""
    with open(wav, "rb") as f:
        return openai_client.audio.transcriptions.create(model=STT_MODEL, file=f).text.strip()

def speak(text, use_eleven):
    text = text.replace("*", "").replace("#", "").replace("`", "")  # strip markdown for the voice
    eleven_key = config.secret("elevenlabs")
    if use_eleven and eleven_key:
        try:
            import urllib.request
            # Stream: play audio as chunks arrive (first sound ~1s) instead of
            # waiting for the whole file. Flash model = lowest TTS latency.
            url = (f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE}/stream"
                   "?optimize_streaming_latency=3")
            body = json.dumps({"text": text, "model_id": "eleven_flash_v2_5"}).encode()
            req = urllib.request.Request(url, data=body, method="POST", headers={
                "xi-api-key": eleven_key,
                "Content-Type": "application/json", "Accept": "audio/mpeg"})
            player = subprocess.Popen(
                ["ffplay", "-autoexit", "-nodisp", "-loglevel", "quiet", "-i", "pipe:0"],
                stdin=subprocess.PIPE)
            with urllib.request.urlopen(req, timeout=20) as resp:
                for chunk in iter(lambda: resp.read(4096), b""):
                    player.stdin.write(chunk)
            player.stdin.close()
            player.wait()
            return
        except Exception as e:
            LOG(f"eleven stream failed -> say: {e}")
    subprocess.run(["say", text])


# --- brain (DeepSeek via OpenRouter, OpenAI-style tool loop) -----------------
def run_shell(command):
    print(f"  $ {command}")
    try:
        r = subprocess.run(command, shell=True, cwd=WORKSPACE,
                           capture_output=True, text=True, timeout=30)
        return (r.stdout + r.stderr)[:6000] or "(no output)"
    except Exception as e:
        return f"error: {e}"

def think(history, user_text, context):
    history.append({"role": "user",
                    "content": f"[What I'm looking at:\n{context}\n]\n\n{user_text}"})
    empties = 0
    for _ in range(8):
        resp = router.chat.completions.create(
            model=LLM_MODEL, max_tokens=1024, temperature=0.3,
            messages=[{"role": "system", "content": SYSTEM}] + history, tools=TOOLS)
        msg = resp.choices[0].message
        if msg.tool_calls:
            history.append(msg.model_dump(exclude_none=True))
            for tc in msg.tool_calls:
                args = json.loads(tc.function.arguments or "{}")
                out = run_shell(args.get("command", ""))
                history.append({"role": "tool", "tool_call_id": tc.id, "content": out})
            empties = 0
            continue
        content = (msg.content or "").strip()
        if content:
            history.append({"role": "assistant", "content": content})
            return content
        # DeepSeek/OpenRouter occasionally returns a blank completion — retry,
        # don't pollute history with the empty turn.
        empties += 1
        if empties >= 3:
            break
    return "I didn't catch that — could you say it again?"


# --- menubar app ------------------------------------------------------------
class VoiceAgent(rumps.App):
    def __init__(self):
        super().__init__("🎙", quit_button=None)
        self.rec = Recorder()
        self.recording = False
        self.recording_source = None
        self.busy = False
        self.use_eleven = config.get("ptt.voice_engine", "elevenlabs") == "elevenlabs"
        self.status = "idle"
        self._setup_checked = False
        self.history = []
        # live conversation mode (Realtime API) — set from the hotkey thread,
        # reconciled onto the main thread in _tick (AppKit isn't thread-safe).
        self.live_on = False
        self.live = None
        self.pill = None
        self._pill_shown = False
        self._press_t = 0.0
        self._ctrl_press_t = 0.0
        self._ctrl_clean = False     # True while a Control press has no other key with it
        self._last_ctrl_tap = 0.0
        self.talk_item = rumps.MenuItem("🔴 Start talking", callback=self.toggle_talk)
        self.voice_item = rumps.MenuItem(
            f"Voice: {'ElevenLabs' if self.use_eleven else 'macOS say'}", callback=self.toggle_voice)
        self.shell_item = rumps.MenuItem("Agentic shell (runs commands)", callback=self.toggle_shell)
        self.shell_item.state = 1 if config.get("live.agentic_shell", False) else 0
        self.menu = [
            self.talk_item,
            rumps.MenuItem("🎧 Live conversation (double-tap Control)", callback=lambda _: self.toggle_live()),
            None,
            self.voice_item,
            self.shell_item,
            None,
            rumps.MenuItem("Set OpenAI key…", callback=lambda _: self._set_key("openai", "OpenAI API key (sk-…)")),
            rumps.MenuItem("Set OpenRouter key…", callback=lambda _: self._set_key("openrouter", "OpenRouter API key (optional)")),
            rumps.MenuItem("Set ElevenLabs key…", callback=lambda _: self._set_key("elevenlabs", "ElevenLabs API key (optional)")),
            rumps.MenuItem("Run setup again…", callback=lambda _: self._onboard(force=True)),
            None,
            rumps.MenuItem("Reset conversation", callback=self.reset),
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
        print("Hotkey ready: hold Right-Option to talk, release to send.")

    ICONS = {"idle": "🎙", "listening": "🔴", "thinking": "💭", "speaking": "🗣"}

    def _tick(self, _):
        if not self._setup_checked:        # first-run onboarding, once the app loop is live
            self._setup_checked = True
            self._onboard()
        self.title = self.ICONS.get(self.status, "🎙")
        talk_title = "⏹ Stop & send" if self.recording else "🔴 Start talking"
        if self.talk_item.title != talk_title:
            self.talk_item.title = talk_title
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

    def toggle_voice(self, item):
        self.use_eleven = not self.use_eleven
        item.title = f"Voice: {'ElevenLabs' if self.use_eleven else 'macOS say'}"
        config.set_("ptt.voice_engine", "elevenlabs" if self.use_eleven else "say")

    def toggle_shell(self, item):
        on = not bool(item.state)
        item.state = 1 if on else 0
        config.set_("live.agentic_shell", on)
        rumps.notification("Thrivbe Voice", f"Agentic shell {'ON' if on else 'OFF'}",
                           "Restart live conversation to apply." if on else
                           "Live mode will only answer, not run commands.")

    def reset(self, _):
        self.history = []
        rumps.notification("Voice Agent", "", "Conversation reset")

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

    def toggle_talk(self, _):
        """Fallback click-to-talk menu action."""
        if not self.recording:
            self._start_recording("menu")
        else:
            self._stop_and_send(self.recording_source)

    # Right-Option does double duty: a long press (>=250ms) is push-to-talk;
    # two quick taps within 400ms toggle live conversation mode.
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
                return
            # Any non-Control key cancels a pending Control double-tap and marks
            # the current Control press "dirty" (it's part of a shortcut, e.g. ⌃C).
            self._ctrl_clean = False
            self._last_ctrl_tap = 0.0
            if key != keyboard.Key.alt_r:
                return
            # Right-Option = push-to-talk. Defer the record start so only a real
            # HOLD (>=TAP_MAX) touches ffmpeg.
            self._press_t = time.monotonic()
            if self.live_on:
                return
            self._hold_timer = threading.Timer(self.TAP_MAX, self._begin_hold_recording)
            self._hold_timer.daemon = True
            self._hold_timer.start()
        except Exception as e:
            LOG(f"key press error: {e!r}")

    def _begin_hold_recording(self):
        try:
            if not self.live_on:
                self._start_recording("hotkey")
        except Exception as e:
            LOG(f"begin-hold-recording error: {e!r}")

    def _on_key_release(self, key):
        try:
            if self._is_ctrl(key):
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
                return
            if key != keyboard.Key.alt_r:
                return
            t = getattr(self, "_hold_timer", None)
            if t is not None:
                t.cancel()
            if self.recording and self.recording_source == "hotkey":
                LOG("Right-Option HOLD released -> send")
                self._stop_and_send("hotkey")
        except Exception as e:
            LOG(f"key release error: {e!r}")

    def _cancel_recording(self):
        """Stop a recording without sending it (used to discard a tap)."""
        self.recording = False
        self.recording_source = None
        self.status = "idle"
        try:
            self.rec.stop()
        except Exception:
            pass

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
        # starting: make sure push-to-talk isn't mid-turn
        if self.recording:
            self._cancel_recording()
        if self.busy:
            LOG("LIVE: busy with a push-to-talk turn — not starting")
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

    def _start_recording(self, source):
        if self.busy or self.recording:
            return False
        self.recording = True
        self.recording_source = source
        self.status = "listening"
        self.rec.start()
        print(f"Recording started by {source}.")
        return True

    def _stop_and_send(self, source):
        if self.busy or not self.recording:
            return False
        if source and self.recording_source != source:
            return False
        self.recording = False
        self.recording_source = None
        print("Recording stopped; transcribing.")
        threading.Thread(target=self._turn, daemon=True).start()
        return True

    def _turn(self):
        self.busy = True
        try:
            wav = self.rec.stop()
            size = os.path.getsize(wav) if wav and os.path.exists(wav) else 0
            LOG(f"turn: wav={wav} size={size} bytes")
            self.status = "thinking"
            text = transcribe(wav)
            LOG(f"turn: transcript={text!r}")
            if not text:
                LOG("turn: empty transcript — likely no mic audio (grant Microphone to Thrivbe Voice)")
                self.status = "idle"
                return
            ctx = grab_context()
            answer = think(self.history, text, ctx)
            LOG(f"turn: answer={answer!r}")
            self.status = "speaking"
            speak(answer, self.use_eleven)
            LOG("turn: spoke answer")
        except Exception as e:
            LOG(f"turn ERROR: {e!r}")
            speak("Sorry, something went wrong.", self.use_eleven)
        finally:
            self.busy = False
            self.status = "idle"


if __name__ == "__main__":
    # realtime.py does `from agent import grab_context, run_shell, SYSTEM, LOG`.
    # When this file runs as __main__, alias it as `agent` so that import resolves
    # to THIS already-initialised module instead of re-importing (which would
    # re-run detect_mic/load_env and build a second OpenAI client).
    sys.modules.setdefault("agent", sys.modules[__name__])
    VoiceAgent().run()
