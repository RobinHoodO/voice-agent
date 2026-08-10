#!/usr/bin/env python3
"""Thrivbe Voice — a hands-free macOS menubar voice agent.

Lives in the menu bar (🎙). **Double-tap Control** to start/stop a live, hands-free
conversation: OpenAI Realtime speech-to-speech (core.live_session), with barge-in, an
optional agentic shell, cross-session memory, and awareness of what's under your
cursor. One mode, one voice (OpenAI) — no push-to-talk, no ElevenLabs.
"""
import json, os, queue, socket, sys, subprocess, threading, time

# py2app puts the frozen python312.zip ahead of Contents/Resources on sys.path, so
# `import core.live_session / mac.pill / core.config` would load STALE zipped copies.
# Put the repo/Resources root (the parent of this package) first so loose copies win —
# this also lets us deploy edits with cp + relaunch (no rebuild, no TCC re-grant).
try:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
except Exception:
    pass

import rumps
import rumps.rumps as rumps_core
from pynput import keyboard

from core import caps, config, floor
from mac import caps_install

# --- diagnostics: log to the app's own log dir (out of /tmp for the product)
LOG_PATH = config.LOG_PATH
def LOG(msg):
    try:
        config.ensure_dirs()
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass


# Hand `core` this machine: the Keychain, the clipboard, the screen, PortAudio, and
# LOG itself. Must happen before anything in core runs — core has no way to reach any
# of it on its own, and silently degrades (no log, no context) if this is skipped.
caps_install.install(log_sink=LOG)


def _prune_old_tasks() -> None:
    """Drop stale task sidecars before launch seeding can silence fresh tids."""
    try:
        config.ensure_dirs()
        cutoff = time.time() - 7 * 24 * 60 * 60
        removed = 0
        for name in os.listdir(config.TASKS_DIR):
            path = os.path.join(config.TASKS_DIR, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.unlink(path)
                    removed += 1
            except Exception as e:
                LOG(f"task prune skipped {name!r}: {e!r}")
        if removed:
            LOG(f"task prune removed {removed} stale sidecars")
    except Exception as e:
        LOG(f"task prune failed: {e!r}")


def _in_quiet_hours() -> bool:
    """True if auto-wake-and-speak on a finished background task should stay silent
    right now (config.live.quiet_hours). Doesn't affect user-initiated (double-tap) live
    sessions — only the unattended announce path that spoke unprompted at 3am."""
    cfg = config.get("live.quiet_hours", {})
    if not cfg or not cfg.get("enabled", False):
        return False
    now = time.localtime()
    if cfg.get("weekdays_only", True) and now.tm_wday >= 5:   # Sat=5, Sun=6
        return False

    def to_min(hhmm, fallback):
        try:
            h, m = hhmm.split(":")
            return int(h) * 60 + int(m)
        except Exception:
            return fallback

    cur = now.tm_hour * 60 + now.tm_min
    start = to_min(cfg.get("start", "00:00"), 0)
    end = to_min(cfg.get("end", "07:00"), 420)
    if start <= end:
        return start <= cur < end
    return cur >= start or cur < end   # window wraps past midnight


def _seed_announced_tasks() -> set:
    try:
        config.ensure_dirs()
        return {f[:-5] for f in os.listdir(config.TASKS_DIR) if f.endswith(".done")}
    except Exception as e:
        LOG(f"task seed failed: {e!r}")
        return set()


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
        # The phone surface: this Mac's brain, reached from Robin's iPhone over the
        # tailnet (mac/phone_surface.py). Built lazily and left OFF unless config says
        # otherwise — see _start_phone_if_enabled at the end of __init__.
        self.phone = None
        self._phone_item = rumps.MenuItem("📱 Phone surface: off",
                                          callback=lambda _: self._toggle_phone())
        self._phone_link_item = rumps.MenuItem("Copy phone link + token",
                                               callback=lambda _: self._copy_phone_link())
        # Slim menu — everything configurable now lives in the Settings window.
        self.menu = [
            rumps.MenuItem("🎧 Live conversation (double-tap Control)", callback=lambda _: self.toggle_live()),
            self._phone_item,
            self._phone_link_item,
            rumps.MenuItem("⚙ Settings…", callback=lambda _: self._open_settings()),
            None,
            rumps.MenuItem("Quit", callback=rumps.quit_application),
        ]
        # Auto-wake: detached delegate jobs drop a .done sentinel in config.TASKS_DIR
        # when they finish. Seed _announced with any that already exist so we don't
        # replay stale results on launch, then poll for new ones.
        self._spoken_tasks = queue.Queue()
        self._announcing = set()
        _prune_old_tasks()
        self._announced = _seed_announced_tasks()
        # reflect status into the menubar icon from the main thread
        rumps.Timer(self._tick, 0.3).start()
        rumps.Timer(self._check_tasks, 2.0).start()
        # OS delegations: .osrun sidecars are watched against the kernel's /status
        # window and converted into the same .out/.done sentinels herdr tasks use,
        # so _check_tasks above speaks them with zero extra plumbing.
        self._kernel_poll_inflight = False
        self._urgent_snapshot = None      # written by the poll thread, read on _tick
        self._woken_keys = None           # None = unseeded; first poll seeds silently
        rumps.Timer(self._check_kernel, 15.0).start()
        rumps.Timer(self._pump_pill_level, 0.05).start()   # feed mic level into the wave
        self.hotkey_listener = keyboard.Listener(
            on_press=self._on_key_press,
            on_release=self._on_key_release,
        )
        self.hotkey_listener.start()
        trusted = accessibility_trusted()
        LOG(f"STARTED. accessibility_trusted={trusted}  log={LOG_PATH}")
        # A journal left on disk means the last run died mid-conversation (crash, force
        # quit, power loss) without reaching the `finally` that stores transcripts. Fold
        # it into the DB now so those words aren't lost — see memory.journal_recover.
        try:
            from core import memory
            if memory.journal_recover():
                config.activity("💾  recovered a conversation from the previous run")
        except Exception as e:
            LOG(f"journal recovery failed: {e!r}")
        self._start_phone_if_enabled()
        if trusted is False:
            LOG("NOT TRUSTED — grant 'Thrivbe Voice' in Accessibility + Input Monitoring, then relaunch.")
            rumps.notification("Thrivbe Voice", "Permission needed",
                               "Enable 'Thrivbe Voice' in Accessibility + Input Monitoring, then relaunch.")
        print("Ready: double-tap Control for a live conversation.")

    ICONS = {"idle": "🎙", "listening": "🔴", "thinking": "💭", "acting": "⚙️", "speaking": "🗣"}
    KERNEL_TUNNEL_ITEM = "Kernel tunnel down — check hetzner-tunnels"

    # This desk's claim on the conversation floor (core/floor.py). One string, because
    # there is one desk: a second double-tap is the same claimant, not a rival.
    FLOOR_KEY = "desk:menubar"

    # --- the phone surface ---------------------------------------------------
    def _phone(self):
        """The PhoneSurface, built on first use. Importing it pulls in FastAPI/uvicorn,
        which the app should not pay for at launch when the surface is off."""
        if self.phone is None:
            from mac.phone_surface import PhoneSurface
            self.phone = PhoneSurface()
        return self.phone

    def _start_phone_if_enabled(self):
        """Honour `phone_surface.enabled` at launch, so a relaunch does not silently
        take Robin's phone away from him."""
        if not config.get("phone_surface.enabled", False):
            return
        try:
            url = self._phone().start()
            LOG(f"phone surface restored at launch: {url}")
        except Exception as e:
            LOG(f"phone surface failed to start at launch: {e!r}")
            rumps.notification("Thrivbe Voice", "Phone surface did not start", str(e))
        self._reconcile_phone_menu()

    def _toggle_phone(self):
        """Menu action: switch the phone surface on or off. Requirement: Robin can see
        that it is on, and turn it off, without a terminal."""
        # `.get`, not `[...]`: this runs on the AppKit main thread from a menu click, and
        # a KeyError here is a traceback in the log and a menu that appears to do nothing.
        status = self._phone().toggle()
        self._reconcile_phone_menu()
        if status.get("on") and status.get("url"):
            rumps.notification("Thrivbe Voice", "Phone surface on",
                               f"{status['url']} — 'Copy phone link + token' for the token")
        elif not status.get("on") and not status.get("error"):
            rumps.notification("Thrivbe Voice", "Phone surface off", "")

    def _copy_phone_link(self):
        """Put the URL and the access token on the clipboard — the once-per-phone setup.

        Deliberately not shown in the menu title: the title is visible over Robin's
        shoulder in every screen share, and a shared secret is not a status line.
        """
        try:
            from mac import phone_surface
            token = phone_surface.ensure_token()
            url = self._phone().url or "(surface is off — turn it on first)"
            caps.clipboard().put_text(f"{url}\ntoken: {token}", paste=False)
            rumps.notification("Thrivbe Voice", "Copied", "Phone link + token on the clipboard")
        except Exception as e:
            LOG(f"copy phone link failed: {e!r}")
            rumps.notification("Thrivbe Voice", "Could not copy the phone link", str(e))

    def _reconcile_phone_menu(self):
        """Repaint the phone menu item from the surface's own status. Main thread."""
        try:
            status = self._phone().status() if self.phone is not None else {"on": False}
        except Exception:
            return
        if status.get("error") and not status.get("on"):
            title = f"📱 Phone surface: {status['error'][:48]}"
        elif not status.get("on"):
            title = "📱 Phone surface: off"
        elif status.get("in_conversation"):
            title = "📱 Phone surface: in conversation — click to turn off"
        else:
            title = f"📱 Phone surface: on · {status.get('tls_port')} — click to turn off"
        if self._phone_item.title != title:
            self._phone_item.title = title

    # --- the conversation floor ----------------------------------------------
    def _evicted_from_floor(self):
        """The phone took the conversation over (Robin tapped "Take over" there).

        Ends this desk session through the SAME `stop()` a spoken sign-off uses, so the
        transcript is persisted by the normal path. Called from the server's event loop
        thread, so it touches plain attributes only — the pill is reconciled in _tick.
        """
        LOG("floor: the phone took over — ending the desk conversation")
        live = self.live
        if live is not None:
            try:
                live.stop()
            except Exception as e:
                LOG(f"desk stop on eviction failed: {e!r}")
        self._auto_stopped()
        caps.notify("Pam moved to your phone — the desk conversation was ended.")

    def _open_settings(self):
        try:
            from mac import settings
            settings.open_settings(self)
        except Exception as e:
            LOG(f"open settings failed: {e!r}")

    def _tick(self, _):
        if not self._setup_checked:        # first-run onboarding, once the app loop is live
            self._setup_checked = True
            self._onboard()
        now = time.monotonic()
        if now >= getattr(self, "_tunnel_next_check", 0.0) and not getattr(self, "_tunnel_probe_inflight", False):
            self._tunnel_next_check = now + 60.0
            self._tunnel_probe_inflight = True
            threading.Thread(target=self._probe_kernel_tunnel, daemon=True).start()
        result = getattr(self, "_tunnel_probe_result", None)
        if result is not None:
            self._tunnel_probe_result = None
            self._tunnel_probe_inflight = False
            if result != getattr(self, "_tunnel_up", None):
                self._tunnel_up = result
                self._reconcile_kernel_tunnel_state()
        title = "🎙⚠" if getattr(self, "_tunnel_up", None) is False else self.ICONS.get(self.status, "🎙")
        if self.title != title:
            self.title = title
        self._reconcile_pill()
        self._reconcile_phone_menu()

    def _probe_kernel_tunnel(self):
        """Background-only TCP check; _tick applies its result on the AppKit thread."""
        try:
            with socket.create_connection(("127.0.0.1", 8790), timeout=0.5):
                up = True
        except OSError:
            up = False
        self._tunnel_probe_result = up

    def _reconcile_kernel_tunnel_state(self):
        """Apply a tunnel transition to the menu. Called only from _tick on the main thread."""
        item = getattr(self, "_kernel_tunnel_item", None)
        if self._tunnel_up:
            if item is not None:
                try:
                    del self.menu[self.KERNEL_TUNNEL_ITEM]
                except Exception as e:
                    LOG(f"kernel tunnel menu remove failed: {e!r}")
                self._kernel_tunnel_item = None
            return
        if item is None:
            item = rumps.MenuItem(self.KERNEL_TUNNEL_ITEM, callback=None)
            self.menu.insert_before("Quit", item)
            self._kernel_tunnel_item = item

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
                    from mac.pill import Pill
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
            # Capture the ID via the tab `do script` actually returns, not "window 1"
            # (frontmost) — a concurrent osascript call opening another Terminal window
            # (e.g. a delegate task) can interleave and steal frontmost first, making
            # "window 1" resolve to the WRONG window (see _close_activity_window guard).
            script = (
                'tell application "Terminal"\n'
                ' activate\n'
                f' set _tab to do script "clear; tail -n 50 -f \\"{path}\\""\n'
                ' return id of (first window whose tabs contains _tab)\n'
                'end tell')
            r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
            self._term_win_id = r.stdout.strip() or None
        except Exception as e:
            LOG(f"activity window failed: {e!r}")

    def _close_activity_window(self):
        """Close the Activity window when the conversation ends (if we opened one).
        Delegate tasks live in herdr lanes now (tools.py), so this Activity window is
        the only Terminal window the app owns — no cross-tracker close race remains."""
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
    def _task_spoken(self, tid):
        """Realtime-loop callback; queue main-thread acknowledgement after injection."""
        try:
            self._spoken_tasks.put_nowait(tid)
        except Exception as e:
            LOG(f"task spoken queue failed: {e!r}")

    def _check_tasks(self, _):
        """Main-thread poll: when a delegated job drops a .done sentinel, wake the
        agent (if idle) to speak its result. Live sessions queue results for their next
        turn boundary; ponytail: mark announced only after speech is injected."""
        try:
            while True:
                try:
                    spoken_tid = self._spoken_tasks.get_nowait()
                    self._announced.add(spoken_tid)
                    self._announcing.discard(spoken_tid)
                except queue.Empty:
                    break
            for f in sorted(os.listdir(config.TASKS_DIR)):
                if not f.endswith(".done"):
                    continue
                tid = f[:-5]
                if tid in self._announced or tid in self._announcing:
                    continue
                # Whoever holds the floor gets the result, which now includes the phone:
                # a job finishing while Robin is on the sofa belongs in the conversation
                # he is actually in, not spoken at an empty room from the Mac's speaker.
                live = self.live if self.live_on else floor.session()
                if live is not None:
                    if live.offer_task(tid, self._read_task_out(tid)):
                        LOG(f"task {tid} done while live — queued for next turn")
                    continue
                if _in_quiet_hours():
                    continue   # leave un-announced; retried each tick until quiet hours end
                LOG(f"task {tid} done -> waking to speak result")
                if self._wake_and_speak(self._read_task_out(tid), tid):
                    self._announcing.add(tid)
        except FileNotFoundError:
            pass
        except Exception as e:
            LOG(f"task check failed: {e!r}")

    def _check_kernel(self, _):
        """Main-thread tick (15s): poll the kernel on a daemon thread when there is a
        reason to — OS delegation sidecars in flight, or proactive wake enabled —
        mirroring _probe_kernel_tunnel so AppKit never blocks on HTTP. Costs one
        listdir when nothing is delegated and the toggle is off."""
        try:
            self._apply_urgent_snapshot()
            if self._kernel_poll_inflight:
                return
            sidecars = [f for f in os.listdir(config.TASKS_DIR) if f.endswith(".osrun")]
            if not sidecars and not config.get("live.proactive_wake", False):
                return
            self._kernel_poll_inflight = True
            threading.Thread(target=self._poll_kernel_runs, args=(sidecars,),
                             daemon=True).start()
        except FileNotFoundError:
            pass
        except Exception as e:
            LOG(f"kernel check failed: {e!r}")

    def _apply_urgent_snapshot(self):
        """Main-thread only. The poll thread leaves the latest urgent items in
        _urgent_snapshot; this applies the deterministic gates and wakes at most once
        per item. The first snapshot after launch only seeds — a restart never replays
        what was already pending."""
        snapshot, self._urgent_snapshot = getattr(self, "_urgent_snapshot", None), None
        if snapshot is None:
            return
        if self._woken_keys is None:
            self._woken_keys = {key for key, _ in snapshot}
            return
        if not config.get("live.proactive_wake", False):
            return
        new = [(k, t) for k, t in snapshot if k not in self._woken_keys]
        if not new:
            return
        # Idle-only: never barge into a live conversation — on EITHER surface, which is
        # what `floor.busy()` adds. Not marked as woken, so the item retries on a later
        # tick, once quiet hours end or the conversation closes.
        if self.live_on or floor.busy() or _in_quiet_hours():
            return
        from core import kernel_tools
        LOG(f"urgent wake: {len(new)} new kernel item(s)")
        config.activity(f"🔔  urgent from the kernel — waking ({len(new)} item(s))")
        if self._wake_and_speak(kernel_tools.urgent_wake_text(new)):
            self._woken_keys.update(k for k, _ in new)

    def _poll_kernel_runs(self, sidecars):
        """Background thread: convert finished OS runs into .out + .done sentinels.
        Only file I/O and HTTP here — _check_tasks announces on its own main-thread
        tick, so this thread never touches AppKit. Also snapshots urgent attention
        items for _apply_urgent_snapshot (main thread) to act on."""
        try:
            from core import kernel_tools
            try:
                status = kernel_tools.kernel_status_raw()
            except Exception:
                return                      # tunnel down — retry on a later tick
            runs = status.get("runs", []) or []
            self._urgent_snapshot = kernel_tools.kernel_urgent(status)
            for name in sidecars:
                path = os.path.join(config.TASKS_DIR, name)
                tid = name[:-6]
                try:
                    with open(path, encoding="utf-8") as fh:
                        sidecar = json.load(fh)
                except Exception as e:
                    LOG(f"osrun sidecar {name} unreadable ({e!r}) — dropping")
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
                    continue
                text = kernel_tools.osrun_outcome(sidecar, runs)
                if text is None:
                    continue                # still running
                out = os.path.join(config.TASKS_DIR, f"{tid}.out")
                done = os.path.join(config.TASKS_DIR, f"{tid}.done")
                with open(out, "w", encoding="utf-8") as fh:
                    fh.write(text)
                with open(done, "w", encoding="utf-8"):
                    pass
                os.unlink(path)             # sentinel written — this run is resolved
                LOG(f"osrun {tid} (run {sidecar.get('runId')}) -> sentinel written")
        except Exception as e:
            LOG(f"kernel run poll failed: {e!r}")
        finally:
            self._kernel_poll_inflight = False

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

    def _notify_live_start_failed(self, detail, task_text=None):
        """Surface a live-start failure when there is no session available to speak it."""
        try:
            body = f"Task result (not spoken): {task_text}" if task_text else str(detail)
            rumps.notification("Thrivbe Voice", "Live session failed to start", body[:200])
        except Exception as notify_err:
            LOG(f"live-start-failed notification also failed: {notify_err!r}")

    def _wake_and_speak(self, text, announce_tid=None):
        """Open a live session that speaks `text` first, then stays listening so the
        user can follow up and close it manually. Mirrors toggle_live's start path, but
        retries one generic start failure before notifying Robin."""
        if config.get("ui.show_terminal", False):
            config.reset_activity()
            self._open_activity_window()
        for attempt in range(2):
            try:
                from core import live_session as realtime
                self.live = realtime.LiveSession(on_state=self._on_live_state, announce=text,
                                                 on_auto_stop=self._auto_stopped,
                                                 on_task_spoken=self._task_spoken,
                                                 announce_tid=announce_tid)
                # `claim`, NOT `take`: nobody asked for this. An auto-wake is the agent's
                # own idea, and the floor's rule is that only Robin ends a conversation.
                # Refused means the caller leaves the item un-announced and retries on a
                # later tick, exactly as it already does during quiet hours.
                floor.claim(floor.DESK, self.FLOOR_KEY, session=self.live,
                            on_evict=self._evicted_from_floor)
                self.live_on = True
                self.status = "listening"
                self.live.start()
                return True
            except floor.Busy as busy:
                LOG(f"wake-and-speak deferred — {busy.holder.surface} has the floor")
                self.live = None
                self.live_on = False
                self.status = "idle"
                self._close_activity_window()
                return False
            except Exception as e:
                LOG(f"wake-and-speak failed: {e!r}")
                floor.release(self.FLOOR_KEY)
                self.live_on = False
                self.status = "idle"
                if attempt == 0:
                    continue
                self._notify_live_start_failed(e, task_text=text)
                return False

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
            self._announcing.clear()
            self.status = "idle"
            floor.release(self.FLOOR_KEY)
            self._close_activity_window()   # detached background jobs keep running
            return
        LOG("LIVE: starting")
        if config.get("ui.show_terminal", False):
            config.reset_activity()         # fresh feed for this conversation
            self._open_activity_window()
        try:
            from core import live_session as realtime
            self.live = realtime.LiveSession(on_state=self._on_live_state,
                                             on_auto_stop=self._auto_stopped,
                                             on_task_spoken=self._task_spoken)
            # `take`, not `claim`: a double-tap of Control is Robin, at this keyboard,
            # deliberately. That is exactly the human act the floor allows to end
            # another conversation — and it ends the phone's through its own stop(),
            # so nothing is dropped mid-flight that a sign-off would have kept.
            floor.take(floor.DESK, self.FLOOR_KEY, session=self.live,
                       on_evict=self._evicted_from_floor)
            self.live_on = True
            self.status = "listening"
            self.live.start()
        except Exception as e:
            LOG(f"LIVE start failed: {e!r}")
            self._notify_live_start_failed(e)
            floor.release(self.FLOOR_KEY)
            self.live_on = False
            self.status = "idle"

    def _auto_stopped(self):
        """The session's idle/max watchdog ended itself — reconcile menu state so the next
        double-tap starts fresh instead of trying to stop an already-dead session. Plain
        attributes only (called off the realtime thread); the pill is reconciled in _tick."""
        self.live_on = False
        self.live = None
        self._announcing.clear()
        self.status = "idle"
        # Give the floor back. Keyed, so if the phone has already taken it this is the
        # no-op it should be rather than a clear of someone else's claim.
        floor.release(self.FLOOR_KEY)
        self._close_activity_window()

    def _on_live_state(self, state):
        """Called from the realtime thread — only mutate plain attributes here."""
        self.status = state


def main():
    """Entry point. `agent.py` at the repo root is the py2app script and calls this."""
    # Nothing does `from agent import LOG` any more (that is core.caps' log sink), but
    # keep the alias: it costs nothing and makes an accidental re-import a no-op rather
    # than a second module that re-runs load_env().
    sys.modules.setdefault("agent", sys.modules[__name__])
    VoiceAgent().run()


if __name__ == "__main__":
    main()
