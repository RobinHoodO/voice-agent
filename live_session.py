"""LiveSession — one live Realtime conversation: orchestration only.

The split-out collaborators live in: backends/ (wire protocol per provider),
realtime_client.py (OpenAI constants/auth), audio.py (AudioMixin: mic/playback),
shell.py (Shell), tools.py (TOOLS + helpers), live_prompt.py (instructions),
macos_context.py (screen capture). This module wires them together and runs the
asyncio event loop + the normalized-event handler.
"""
import asyncio
import json
import os
import queue
import re
import subprocess
import threading
import time
import uuid

import config
import kernel_tools
from audio import AudioMixin
from backends import base as events
from backends.openai_backend import OpenAIBackend
from live_prompt import _build_live_instructions
from realtime_client import VOICE, _selftest
from shell import Shell
import services
from tools import (TOOLS, LOCAL_HIGH_STAKES, _extract_json, _put_text,
                   delegate_task, fleet, continue_task, close_finished_tasks, close_gate)


# Rough all-in OpenAI Realtime audio estimate; tune without code changes if billing shifts.
# Reconciled 2026-07-30 against actual OpenAI usage ($2.49 / 26.15 NOK for 16.6 min that day
# the old 3.0 default estimated at 49.73 NOK — ~1.9x too high) — see project_voice_agent memory.
VOICE_REALTIME_NOK_PER_MIN = float(os.getenv("VOICE_REALTIME_NOK_PER_MIN", "1.6"))

# Tools that hand free text to a background agent, and how to frame it:
# {tool: (args field, label, attach the memory pointer?)}. continue_task goes into a
# lane already mid-task with full context — and flattens newlines — so no pointer there.
DELEGATING_TOOLS = {
    "delegate":     ("instruction", "Task",        True),
    "os_delegate":  ("instruction", "Instruction", False),
    "continue_task": ("feedback",   "Feedback",    False),
}


def _log(msg: str) -> None:
    try:
        from agent import LOG
        LOG(msg)
    except Exception:
        pass


PENDING_ACTION_TTL_SECONDS = 120
AFFIRM_RE = re.compile(r"\b(yes|yeah|yep|confirm|confirmed|approve|approved|go ahead|do it|send it|proceed|ja|kjør)\b", re.IGNORECASE)
DENY_RE = re.compile(r"\b(no|nope|cancel|reject|rejected|stop|abort|don't|nei)\b", re.IGNORECASE)


def _is_short_affirm(text: str) -> bool:
    return len(text.strip().split()) <= 4 and bool(AFFIRM_RE.search(text))


def _is_short_deny(text: str) -> bool:
    return len(text.strip().split()) <= 4 and bool(DENY_RE.search(text))


# What a confirmed high-stakes tool actually runs. A gated tool with no entry here is
# staged and confirmable but not executable on this surface — handled explicitly rather
# than silently, so a spoken "yes" is never swallowed.
HIGH_STAKES_EXECUTORS = {
    "gmail_send": lambda args: services.gmail_send(args),
    "kernel_decide": lambda args: kernel_tools.kernel_decide(args),
    "close_finished_tasks": lambda args: close_finished_tasks(args, confirmed=True),
}


def _confirmation_preview(tool: str, args: dict) -> str:
    """The sentence the model reads back before Robin says yes. Specific beats generic —
    'send an email to X' is checkable by ear; 'run gmail_send' is not."""
    if tool == "gmail_send":
        return (f"about to send an email to {args.get('to')} "
                f"with subject '{args.get('subject')}'")
    if tool == "kernel_decide":
        return (f"about to record decision {args.get('decision')} for approval "
                f"{args.get('approvalId', args.get('approval_id'))}")
    if tool == "close_finished_tasks":
        return f"about to close the foreign pane '{args.get('task_name')}'"
    known = ", ".join(f"{k}={v}" for k, v in list(args.items())[:3]) or "no arguments"
    return f"about to run {tool.replace('_', ' ')} with {known}"


def _pending_confirmation_outcome(pending: dict, transcript: str, now: float | None = None) -> str:
    """Return the deterministic disposition for a staged kernel decision."""
    now = time.time() if now is None else now
    if now - pending["ts"] >= PENDING_ACTION_TTL_SECONDS:
        return "expired"
    if _is_short_deny(transcript):
        return "denied"
    if _is_short_affirm(transcript):
        return "confirmed"
    return "dropped"


class LiveSession(AudioMixin):
    """One live Realtime conversation. start()/stop() are called from the main
    (rumps) thread; everything else runs on the session's own asyncio thread.
    Audio I/O comes from AudioMixin."""

    def __init__(self, on_state=None, announce=None, on_auto_stop=None, on_task_spoken=None,
                 announce_tid=None):
        self.on_state = on_state or (lambda s: None)
        self._announce = announce          # if set, speak this aloud right after opening
        self._on_auto_stop = on_auto_stop  # called when the idle/max watchdog ends the session
        self._on_task_spoken = on_task_spoken
        self._announce_tid = announce_tid
        self._session_start = 0.0          # loop.time() when this session opened
        self._last_speech = 0.0            # loop.time() of the last detected speech turn
        self._wall_start = 0.0             # wall-clock time when this session opened
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._backend = OpenAIBackend()
        self._mic_q: asyncio.Queue | None = None      # mic bytes -> ws (loop thread)
        self._out_q: queue.Queue = queue.Queue(maxsize=256)  # model audio -> speaker; bounded (drop-oldest)
        self._in_stream = None
        self._out_stream = None
        self._player_thread: threading.Thread | None = None
        self._mic_task: asyncio.Future | None = None  # the mic->ws pump (kept so crashes surface)
        self._running = False
        self._speaking = False
        self._shell: Shell | None = None             # persistent zsh for this session
        self._fn_names: dict = {}                     # call_id -> tool name (from output_item.added)
        self._pending_action: dict | None = None      # the one staged high-stakes action
        # Fail closed until the manifest says otherwise: assume every kernel-backed tool
        # needs confirmation. _configure narrows this to the real highStakes set once the
        # kernel answers; if it never does, we over-confirm instead of under-confirming.
        self._high_stakes: set = set(kernel_tools.KERNEL_TOOL_NAMES) | set(LOCAL_HIGH_STAKES)
        self._turns: list = []                        # ["you: …", "agent: …"] for this conversation
        self._delegated_upto: int = 0                 # cursor into _turns: what's already been handed off
        self.level: float = 0.0                       # live mic level 0..1 (drives the wave pill)
        self._local_speaking = False
        self._local_silence_since: float | None = None
        self._cfg: dict = {}                          # snapshot of config for this session
        self._offered_tasks: queue.Queue = queue.Queue()
        self._offered_tids: set = set()
        self._offered_lock = threading.Lock()

    @property
    def _ws(self):
        # The backend owns the socket; stop()/audio/tests keep reaching it here.
        return self._backend.ws

    @_ws.setter
    def _ws(self, value):
        self._backend.ws = value

    def _make_backend(self, cfg: dict):
        name = (cfg.get("live") or {}).get("backend", "openai")
        if name == "gemini":
            from backends.gemini_backend import GeminiBackend
            return GeminiBackend()
        return OpenAIBackend()

    # ----- lifecycle (called from main thread) -----
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        loop, ws = self._loop, self._ws
        if loop and ws:
            async def _close():
                try:
                    await ws.close()
                except Exception:
                    pass
            try:
                asyncio.run_coroutine_threadsafe(_close(), loop)
            except Exception:
                pass

    async def _idle_watchdog(self) -> None:
        """Auto-end the session so a forgotten-on mic doesn't keep responding to ambient
        speech (e.g. you talking to another app). Two guards, either 0 = off:
          auto_stop_idle_s — stop after this much silence (no speech turn). Default 90s.
          auto_stop_max_s  — hard cap on total live time. Default 300s.
        The idle guard handles walk-aways; the max cap handles 'kept talking nearby so idle
        never fired' — the exact case that triggered this. Adjust/disable in config.json."""
        live = self._cfg.get("live") or {}
        idle_s = float(live.get("auto_stop_idle_s", 90) or 0)
        max_s = float(live.get("auto_stop_max_s", 300) or 0)
        if idle_s <= 0 and max_s <= 0:
            return
        reason = None
        while self._running and reason is None:
            await asyncio.sleep(5)
            now = self._loop.time()
            if idle_s > 0 and (now - self._last_speech) > idle_s:
                reason = f"{int(now - self._last_speech)}s idle (>{int(idle_s)}s)"
            elif max_s > 0 and (now - self._session_start) > max_s:
                reason = f"{int(now - self._session_start)}s live (>{int(max_s)}s cap)"
        if reason is None:
            return
        _log(f"auto-stop: {reason} — ending live session")
        self.stop()
        if self._on_auto_stop:
            try:
                self._on_auto_stop()
            except Exception as e:
                _log(f"on_auto_stop callback failed: {e!r}")

    def _notify(self, msg: str) -> None:
        """Best-effort user-visible notification (so failures aren't silent)."""
        try:
            import rumps
            rumps.notification("Thrivbe Voice", "", msg)
        except Exception:
            pass

    def _on_mic_task_done(self, task) -> None:
        """Surface a crashed mic pump instead of letting asyncio swallow it."""
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        except Exception:
            return
        if exc:
            _log(f"mic pump crashed: {exc!r}")

    # ----- session thread -----
    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        backoff = 1.0
        try:
            while self._running:
                try:
                    self._loop.run_until_complete(self._session())
                    backoff = 1.0          # connected at least once → reset backoff
                except Exception as e:
                    _log(f"realtime session error: {e!r}")
                self._teardown_audio()     # close this attempt's streams/shell before any retry
                if not self._running:
                    break                  # user asked to stop — clean exit
                # Unexpected drop while still live: back off and reconnect, so the daemon
                # doesn't go silently deaf on a network blip / idle timeout / server close.
                self.on_state("reconnecting")
                self._notify(f"Connection lost — reconnecting in {int(backoff)}s")
                _log(f"session dropped; reconnecting in {backoff:.0f}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
        finally:
            self._teardown_audio()
            self._running = False
            self.on_state("idle")
            _log("realtime session closed")
            self._persist_conversation()

    async def _await_transcript(self, timeout: float = 2.0) -> None:
        """Wait (briefly) for Whisper's transcript of the utterance that triggered this
        handoff. Transcription is a side-channel running in parallel with the model's
        response, so ~15% of turns it lands AFTER the tool call — without this, the
        verbatim block would carry the PREVIOUS turn's words under a "Robin's own words"
        label, which is worse than carrying none. Delegation runs for minutes; up to 2s
        here is invisible, and it costs nothing in the common case where it already
        arrived."""
        if any(t.startswith("you: ") for t in self._turns[self._delegated_upto:]):
            return
        deadline = self._loop.time() + timeout
        while self._loop.time() < deadline:
            await asyncio.sleep(0.1)
            if any(t.startswith("you: ") for t in self._turns[self._delegated_upto:]):
                return
        _log("verbatim: transcript did not arrive in time — handing off without it")

    def _verbatim_since_handoff(self) -> str:
        """Robin's own ASR-transcribed turns since the last delegate/continue_task/
        os_delegate call — ground truth, independent of how faithfully the model's
        own tool-call argument reflects what he actually said."""
        raw = [t for t in self._turns[self._delegated_upto:] if t.startswith("you: ")]
        self._delegated_upto = len(self._turns)
        return "\n".join(raw)

    def _wrap_delegate_text(self, instruction: str, label: str = "Task",
                             memory_pointer: bool = True) -> str:
        """Prefix a delegated instruction/feedback with Robin's verbatim recent turns
        (ground truth, independent of how the model phrased its own tool-call argument).
        `claude`/`pi` herdr-pane agents already get MEMORY.md/claude-mem automatically,
        so this points at past voice conversations rather than pre-fetching them —
        pull, not push."""
        parts = []
        raw = self._verbatim_since_handoff()
        if raw:
            parts.append(f"Robin's own words (verbatim):\n{raw}")
        parts.append(f"{label}:\n{instruction}")
        if memory_pointer:
            mem_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory.py")
            parts.append(f"(Past voice conversations are searchable via "
                         f"`python {mem_path} recall \"<query>\"` if relevant.)")
        return "\n\n".join(parts)

    def _persist_conversation(self) -> None:
        """On close, store this conversation's transcript, then summarize it in the
        background (non-blocking) so future sessions can recall it. Best-effort."""
        turns = self._turns
        if not turns:
            return
        transcript = "\n".join(turns)
        duration = max(0.0, time.time() - self._wall_start) if self._wall_start else 0.0
        cost_nok = (duration / 60.0) * VOICE_REALTIME_NOK_PER_MIN
        try:
            import memory
            cid = memory.record(transcript)
            _log(f"conversation stored (id={cid}, {len(turns)} turns)")
            if cid:
                threading.Thread(target=self._learn, args=(cid, transcript),
                                 daemon=True).start()
        except Exception as e:
            _log(f"persist conversation failed: {e!r}")
        if duration > 1.0:
            try:
                kernel_tools.session_log(
                    "voice-agent-realtime", cost_nok, duration, turns[0] or transcript[:80])
            except Exception as e:
                _log(f"session cost log failed: {e!r}")

    def _learn(self, cid: int, transcript: str) -> None:
        """Background: in ONE pi call, extract a summary AND durable learnings from this
        conversation — given the learnings already held, so the model itself dedups and
        flags contradictions (supersede). Stores the summary; applies the learnings.

        Degrades cleanly: if pi is unavailable or the output isn't JSON, the transcript
        still persists and we fall back to a plain-text summary; learnings just no-op.
        Off entirely when live.memory.learn is false (only summarize)."""
        try:
            import memory
            mem_cfg = (self._cfg.get("live") or {}).get("memory") or {}
            learn_on = mem_cfg.get("learn", True)
            model = (self._cfg.get("live") or {}).get("pi_model", "deepseek-v4-flash")
            pi_bin = os.path.expanduser("~/.local/bin/pi")
            if not os.path.exists(pi_bin):
                pi_bin = "pi"
            existing = memory.learnings_block(30) if learn_on else ""
            prompt = (
                "You maintain a long-term memory of the user from voice conversations. "
                "Return STRICT JSON ONLY (no prose, no code fences) with this shape:\n"
                '{"summary": "3-5 short lines: topic, key decisions, open tasks", '
                '"new_learnings": [{"type": "preference|fact|correction", "text": "..."}], '
                '"supersede": [<ids of existing learnings this conversation corrects/replaces>]}\n'
                "Only include GENUINELY NEW durable preferences/facts/corrections about the "
                "user — never repeat anything already in the existing learnings below. If "
                "the user contradicts an existing learning, put its id in supersede AND add "
                "the corrected version to new_learnings. Empty arrays if nothing new.\n\n"
                f"Existing learnings (id: [type] text):\n{existing or '(none yet)'}\n\n"
                f"Conversation transcript:\n{transcript[:8000]}")
            r = subprocess.run([pi_bin, "-p", "--model", model, prompt],
                               stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=120)
            raw = (r.stdout or "").strip()
            if not raw:
                return
            payload = _extract_json(raw)
            if isinstance(payload, dict):
                summary = (payload.get("summary") or "").strip()
                if summary:
                    memory.set_summary(cid, summary)
                if learn_on:
                    memory.extract_apply(payload, cid)
                    try:
                        for item in payload.get("new_learnings") or []:
                            text = item.get("text", "")
                            if text:
                                kernel_tools.kernel_remember(
                                    {"text": f"[voice-learned {item.get('type', 'fact')}] {text}"})
                    except Exception as e:
                        _log(f"kernel memory mirror failed: {e!r}")
                _log(f"conversation {cid} learned "
                     f"(+{len(payload.get('new_learnings') or [])} learnings, "
                     f"-{len(payload.get('supersede') or [])} superseded)")
            else:
                # Not JSON — keep the value: treat the whole output as the summary.
                memory.set_summary(cid, raw[:2000])
                _log(f"conversation {cid} summarized (non-JSON fallback)")
        except Exception as e:
            _log(f"learn failed: {e!r}")

    async def _session(self) -> None:
        self._mic_q = asyncio.Queue()
        self._cfg = config.load()
        self._backend = self._make_backend(self._cfg)
        ws = await self._backend.connect()
        try:
            try:
                self._shell = Shell()
            except Exception as e:
                _log(f"persistent shell start failed: {e!r}")
            await self._configure()
            self._start_audio()
            self._mic_task = asyncio.ensure_future(self._pump_mic())
            self._mic_task.add_done_callback(self._on_mic_task_done)
            self.on_state("listening")
            _log("realtime session open — listening")
            self._session_start = self._last_speech = self._loop.time()
            self._wall_start = time.time()
            wd = asyncio.ensure_future(self._idle_watchdog())
            if self._announce:
                await self._speak_announcement()
            try:
                async for raw in ws:
                    if not self._running:
                        break
                    try:
                        await self._handle(json.loads(raw))
                    except Exception as e:
                        _log(f"realtime handle error: {e!r}")
            finally:
                wd.cancel()
        finally:
            await self._backend.close()

    async def _configure(self) -> None:
        ctx = await self._loop.run_in_executor(None, _grab_context)
        # Narrow the fail-closed gate set to what the kernel actually flags. None means
        # the manifest was unreadable — keep the conservative set from __init__.
        declared = await self._loop.run_in_executor(None, kernel_tools.kernel_high_stakes)
        if declared is None:
            _log(f"high-stakes manifest unavailable — failing closed on "
                 f"{len(self._high_stakes)} tools")
            config.activity("⚠️  gate list unavailable — confirming all kernel actions")
        else:
            self._high_stakes = set(declared) | set(LOCAL_HIGH_STAKES)
            _log(f"high-stakes gate set: {sorted(self._high_stakes)}")
        live = self._cfg.get("live") or {}
        instructions = _build_live_instructions(ctx, self._cfg)
        voice = live.get("voice") or VOICE
        # Respect the agentic-shell toggle. Fail CLOSED — default False to match
        # config.DEFAULTS and the menu, so a missing key never exposes the shell.
        # run_shell + delegate both act on the system, so both are gated; put_text
        # (clipboard/paste) and remember stay available either way.
        tools = TOOLS if live.get("agentic_shell", False) else [
            t for t in TOOLS if t.get("name") not in ("run_shell", "delegate")]
        await self._backend.send_setup(instructions, tools, voice)

    # ----- realtime events -----
    async def _handle(self, ev: dict) -> None:
        ne = self._backend.parse_event(ev)
        await self._handle_normalized(ne)
        for extra in self._backend.drain_extra_events():
            await self._handle_normalized(extra)

    async def _handle_normalized(self, ne) -> None:
        k = ne.kind
        if k == events.RESPONSE_CREATED:
            self._audio_n = 0
        elif k == events.RESPONSE_DONE:
            resp = ne.detail or {}
            status = resp.get("status")
            details = resp.get("status_details")
            _log(f"ev response.done status={status} audio_deltas={getattr(self, '_audio_n', 0)} "
                 f"details={details}")
        if k == events.SPEECH_STARTED:
            await self._on_speech_started()
        elif k == events.SPEECH_STOPPED:
            await self._on_speech_stopped()
        elif k == events.AUDIO_DELTA:
            self._enqueue_audio(ne.audio)
            self._audio_n = getattr(self, "_audio_n", 0) + 1
            if self._audio_n == 1:
                _log("first audio delta -> speaking")
            if not self._speaking:
                self._speaking = True
                self.on_state("speaking")
        elif k == events.AUDIO_DONE:
            self._speaking = False
            self.on_state("listening")
        elif k == events.TOOL_CALL:
            await self._do_tool({"call_id": ne.call_id, "name": ne.name,
                                 "arguments": ne.args})
        elif k == events.USER_TRANSCRIPT:
            heard = ne.text
            if heard:
                await self._resolve_pending_action(heard)
            _log(f"live heard: {heard!r}")
            if heard:
                config.activity(f"🗣  you: {heard}")
                self._turns.append(f"you: {heard}")
        elif k == events.AGENT_TRANSCRIPT:
            said = ne.text
            if said:
                config.activity(f"💬  agent: {said}")
                self._turns.append(f"agent: {said}")
        elif k == events.ERROR:
            _log(f"realtime error event: {ne.detail}")

    async def _on_speech_started(self) -> None:
        self._last_speech = self._loop.time()   # reset the idle watchdog on any turn
        self._flush_out()                       # barge-in: stop talking
        if self._speaking:
            await self._backend.cancel_response()
            self._speaking = False
        self.on_state("listening")

    async def _on_speech_stopped(self) -> None:
        self._last_speech = self._loop.time()   # reset the idle watchdog on any turn
        await self._inject_context_and_respond()

    async def _resolve_pending_action(self, transcript: str) -> None:
        """Execute (or drop) the one staged high-stakes action, per the deterministic
        spoken gate. Which tools land here is data — see self._high_stakes — but the
        gate machinery itself (TTL, affirm/deny regex) is unchanged and deliberate."""
        pending = getattr(self, "_pending_action", None)
        if not pending:
            return
        tool = pending["tool"]
        outcome = _pending_confirmation_outcome(pending, transcript)
        self._pending_action = None
        if outcome != "confirmed":
            _log(f"{tool} confirmation dropped: {outcome}")
            return
        executor = HIGH_STAKES_EXECUTORS.get(tool)
        if executor is None:
            # Gated but not executable from here (e.g. a manifest tool this surface
            # doesn't implement). Say so rather than silently swallowing the yes.
            _log(f"{tool} confirmed but has no executor on this surface")
            config.activity(f"⚠️  {tool} confirmed but not executable here")
            return
        args = pending["args"]
        out = await self._loop.run_in_executor(None, executor, args)
        _log(f"{tool} confirmation executed: {args!r} -> {out}")
        config.activity(f"✅  {tool.replace('_', ' ')} confirmed: {out}")
        if self._ws:
            await self._backend.send_text_context(
                f"[System context: the confirmed {tool} has executed. Result: {out}]")

    async def _speak_announcement(self) -> None:
        """Auto-wake greeting: the menubar watcher opened this session because a
        background task finished. Tell the model to report the result out loud."""
        # Keep the TAIL, not the head: the VERIFIED/UNVERIFIED/FAILED tag is the last line,
        # so truncating from the front would drop the very verdict we want spoken.
        txt = (self._announce or "")[-2500:]
        self._announce = None
        config.activity("🔔  task finished — speaking result")
        await self._backend.send_text_context(
            "[A background task you started earlier just finished. "
            "Greet me briefly and tell me out loud, in 1-3 sentences, "
            "what it found. The result ends with a status tag — "
            "VERIFIED means it confirmed the end-state (say it's done); "
            "UNVERIFIED means it could NOT confirm it (say so plainly — "
            "tell me what couldn't be confirmed, don't imply success); "
            "FAILED means it didn't work. Be honest about which it is. "
            "Result:\n" + txt + "\n]")
        await self._backend.trigger_response()
        tid, self._announce_tid = self._announce_tid, None
        if tid and self._on_task_spoken:
            try:
                self._on_task_spoken(tid)
            except Exception as e:
                _log(f"task spoken callback failed: {e!r}")

    def offer_task(self, tid: str, text: str) -> bool:
        """Queue one completed task for the next natural response boundary."""
        if not tid:
            return False
        with self._offered_lock:
            if tid in self._offered_tids:
                return False
            self._offered_tids.add(tid)
            self._offered_tasks.put((tid, text))
        return True

    def _drain_offered_tasks(self) -> list:
        tasks = []
        while True:
            try:
                tasks.append(self._offered_tasks.get_nowait())
            except queue.Empty:
                return tasks

    async def _inject_context_and_respond(self) -> None:
        self.on_state("thinking")
        # Grab text context and the screenshot concurrently so the silent gap before the
        # reply stays as short as possible (each toggle may no-op and return fast).
        # Cap the grab: it sits in the silent gap between "you stopped talking" and the
        # reply, so a slow AppleScript/screenshot must not stall the turn.
        try:
            ctx, shot = await asyncio.wait_for(asyncio.gather(
                self._loop.run_in_executor(None, _grab_context),
                self._loop.run_in_executor(None, _grab_screenshot),
            ), timeout=2.0)
        except asyncio.TimeoutError:
            _log("context grab timed out (>2s) — replying without screen context")
            ctx, shot = "", ""
        _log(f"context injected ({len(ctx)} chars, screenshot={'yes' if shot else 'no'})")
        await self._backend.send_text_context(
            f"[What I'm looking at right now:\n{ctx}\n"
            + ("(A screenshot of my screen is attached above.)\n" if shot else "")
            + "]",
            image_b64=shot or None)
        finished = self._drain_offered_tasks()
        try:
            if finished:
                preamble = ("One background task finished." if len(finished) == 1 else
                            f"{len(finished)} background tasks finished.")
                results = "\n\n".join(f"Task {tid}:\n{text}" for tid, text in finished)
                await self._backend.send_text_context(
                    f"[{preamble} Work their results naturally into "
                    f"your next spoken reply:\n{results}\n]")
            await self._backend.trigger_response()
        except Exception:
            for task in finished:
                self._offered_tasks.put(task)
            raise
        if finished and self._on_task_spoken:
            for tid, _text in finished:
                try:
                    self._on_task_spoken(tid)
                except Exception as e:
                    _log(f"task spoken callback failed: {e!r}")

    async def _do_tool(self, ev: dict) -> None:
        self.on_state("acting")          # running a tool/command — distinct from thinking
        call_id = ev.get("call_id")
        name = ev.get("name") or self._fn_names.pop(call_id, "") or "run_shell"
        try:
            args = json.loads(ev.get("arguments") or "{}")
        except Exception:
            args = {}
        # Attach Robin's verbatim words BEFORE the elif chain: os_delegate is in the
        # fail-closed high-stakes set, so a branch further down would be unreachable
        # whenever the kernel manifest can't be read. One place, order-independent.
        if name in DELEGATING_TOOLS:
            field, label, pointer = DELEGATING_TOOLS[name]
            await self._await_transcript()
            args = dict(args)
            args[field] = self._wrap_delegate_text(args.get(field, ""), label=label,
                                                   memory_pointer=pointer)
        if name == "remember":
            import memory
            note = args.get("note", "")
            # Unify the old note silo into the learnings store, so there's one memory
            # surface. A free-form remembered note is a durable fact by default.
            lid = memory.add_learning("fact", note)
            out = "noted" if lid else "nothing to remember"
            _log(f"remember: {note!r}")
            config.activity(f"💾  remembered: {note}")
        elif name == "recall":
            import memory
            q = args.get("query", "")
            out = memory.recall(q) or "nothing relevant in memory"
            _log(f"recall: {q!r}")
            config.activity(f"🧠  recalled: {q}")
        elif name == "put_text":
            out = _put_text(args.get("text", ""), args.get("paste", True))
            _log(f"put_text (paste={args.get('paste', True)}): {out}")
            config.activity(f"📋  {out}")
        elif name == "set_prompt":
            text = (args.get("text") or "").strip()
            try:
                config.set_("live.custom_prompt", text)
                self._cfg = config.load()   # so it also applies for the rest of this session
                out = "Saved — I'll use that from now on."
            except Exception as e:
                out = f"couldn't save it: {e}"
            _log(f"set_prompt ({len(text)} chars)")
            config.activity(f"🧠  custom prompt updated ({len(text)} chars)")
        elif name == "delegate":
            out = await self._loop.run_in_executor(
                None, delegate_task, args.get("instruction", ""), self._cfg,
                args.get("task_name", ""), self._run_in_shell, args.get("reuse_pane", ""))
            config.activity(f"🚀  delegated: {args.get('task_name', '') or 'task'}")
        elif name == "fleet":
            out = await self._loop.run_in_executor(None, fleet, args)
            _log(f"fleet: {out}")
            config.activity("🛤  checked herdr fleet")
        elif name == "continue_task":
            out = await self._loop.run_in_executor(None, continue_task, args)
            _log(f"continue_task: {out}")
            config.activity(f"🛤  follow-up → {args.get('task_name', '')[:40]}")
        elif name == "close_finished_tasks":
            needs_confirmation = await self._loop.run_in_executor(None, close_gate, args)
            if needs_confirmation:
                if getattr(self, "_pending_action", None):
                    _log("close_finished_tasks: superseded a previously staged action")
                self._pending_action = {"tool": name, "args": args, "ts": time.time()}
                out = (f"CONFIRMATION REQUIRED: {_confirmation_preview(name, args)}. "
                       "Ask the user to confirm out loud.")
                _log(f"close_finished_tasks staged for confirmation: {args!r}")
                config.activity("⏸  close finished tasks awaiting confirmation")
            else:
                out = await self._loop.run_in_executor(None, close_finished_tasks, args)
                _log(f"close_finished_tasks: {out}")
                config.activity("🛤  closed finished task lanes")
        elif name in self._high_stakes:
            # Irreversible or outward-facing → stage it; nothing runs until Robin says
            # yes out loud. Membership is DATA (kernel manifest highStakes ∪ the local
            # set), so adding a gate is a manifest edit, not a code change here.
            if getattr(self, "_pending_action", None):
                _log(f"{name}: superseded a previously staged action awaiting confirmation")
            self._pending_action = {"tool": name, "args": args, "ts": time.time()}
            out = (f"CONFIRMATION REQUIRED: {_confirmation_preview(name, args)}. "
                   "Ask the user to confirm out loud.")
            _log(f"{name} staged for confirmation: {args!r}")
            config.activity(f"⏸  {name.replace('_', ' ')} awaiting confirmation")
        elif name in ("notion_create_task", "notion_search", "notion_list_tasks",
                      "notion_update_task", "front_search", "front_draft",
                      "gmail_search", "calendar_add", "drive_search"):
            handler = getattr(services, name)
            out = await self._loop.run_in_executor(None, handler, args)
            _log(f"{name}: {out[:120]}")
            config.activity(f"🔗  {name.replace('_', ' ')}")
        elif name == "kernel_status":
            out = await self._loop.run_in_executor(None, kernel_tools.kernel_status)
            _log("kernel_status")
            config.activity("🧠  checked kernel status")
        elif name == "kernel_memo":
            out = await self._loop.run_in_executor(None, kernel_tools.kernel_memo, args)
            _log(f"kernel_memo: {out}")
            config.activity("🧠  filed kernel memo")
        elif name == "os_delegate":
            out = await self._loop.run_in_executor(None, kernel_tools.os_delegate, args)
            _log(f"os_delegate: {out}")
            config.activity("🧠  delegated to OS worker")
        elif name == "kernel_remember":
            out = await self._loop.run_in_executor(None, kernel_tools.kernel_remember, args)
            _log(f"kernel_remember: {out}")
            config.activity("🧠  remembered to ONE memory")
        elif name == "kernel_recall":
            out = await self._loop.run_in_executor(None, kernel_tools.kernel_recall, args)
            _log(f"kernel_recall: {out}")
            config.activity("🧠  recalled from ONE memory")
        elif name == "bloom_create_task":
            out = await self._loop.run_in_executor(None, kernel_tools.bloom_create_task, args)
            _log(f"bloom_create_task: {out}")
            config.activity(f"🧠  {out}")
        elif name == "bloom_update_task":
            out = await self._loop.run_in_executor(None, kernel_tools.bloom_update_task, args)
            _log(f"bloom_update_task: {out}")
            config.activity(f"🧠  {out}")
        elif name == "bloom_comment_task":
            out = await self._loop.run_in_executor(None, kernel_tools.bloom_comment_task, args)
            _log(f"bloom_comment_task: {out}")
            config.activity(f"🧠  {out}")
        elif name == "twenty_search_contacts":
            out = await self._loop.run_in_executor(None, kernel_tools.twenty_search_contacts, args)
            _log(f"twenty_search_contacts: {args.get('name_query', '')!r}")
            config.activity(f"🧠  CRM search: {args.get('name_query', '')}")
        elif name == "semsearch_query":
            out = await self._loop.run_in_executor(None, kernel_tools.semsearch_query, args)
            _log(f"semsearch_query: {args.get('corpus', 'people')}: {args.get('query', '')!r}")
            config.activity(f"🧠  semantic search: {args.get('query', '')}")
        elif name == "hybrid_rag_search":
            out = await self._loop.run_in_executor(None, kernel_tools.hybrid_rag_search, args)
            _log(f"hybrid_rag_search: {args.get('query', '')!r}")
            config.activity(f"🧠  hybrid search: {args.get('query', '')}")
        elif name == "cognee_ask":
            out = await self._loop.run_in_executor(None, kernel_tools.cognee_ask, args)
            _log(f"cognee_ask: {args.get('query', '')!r}")
            config.activity(f"🧠  community graph: {args.get('query', '')}")
        elif name == "graph_get_node":
            out = await self._loop.run_in_executor(None, kernel_tools.graph_get_node, args)
            _log(f"graph_get_node: {args.get('id', '')!r}")
            config.activity(f"🧠  graph node: {args.get('id', '')}")
        elif name == "graph_get_document":
            out = await self._loop.run_in_executor(None, kernel_tools.graph_get_document, args)
            _log(f"graph_get_document: {args.get('id', '')!r}")
            config.activity(f"🧠  graph document: {args.get('id', '')}")
        elif name == "bloom_list_projects":
            out = await self._loop.run_in_executor(None, kernel_tools.bloom_list_projects, args)
            _log("bloom_list_projects")
            config.activity("🧠  listed Bloom projects")
        elif name == "bloom_list_tasks":
            out = await self._loop.run_in_executor(None, kernel_tools.bloom_list_tasks, args)
            _log(f"bloom_list_tasks: {args!r}")
            config.activity("🧠  listed Bloom tasks")
        elif name == "hermes_fleet":
            out = await self._loop.run_in_executor(None, kernel_tools.hermes_fleet, args)
            _log(f"hermes_fleet({args}): {out[:120]}")
            config.activity(f"🐝  hermes fleet: {args.get('pod') or 'all pods'}")
        elif name == "list_inbox_items":
            out = await self._loop.run_in_executor(None, kernel_tools.list_inbox_items, args)
            _log(f"list_inbox_items: {args!r}")
            config.activity("🧠  listed inbox items")
        else:
            out = await self._loop.run_in_executor(None, self._run_in_shell, args.get("command", ""))
        await self._backend.send_tool_result(call_id, out)
        await self._backend.trigger_response()

    def _enqueue_audio(self, chunk: bytes) -> None:
        # Bounded playback queue: if the model outruns the speaker, drop the OLDEST
        # chunk instead of letting the queue (and latency) grow without bound.
        try:
            self._out_q.put_nowait(chunk)
        except queue.Full:
            try:
                self._out_q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._out_q.put_nowait(chunk)
            except queue.Full:
                pass

    def _run_in_shell(self, command: str) -> str:
        if self._shell is None:
            return "error: shell not started"
        _log(f"live $ {command}")
        config.activity(f"⚙️  ran: {command}")
        timeout = float((self._cfg.get("live") or {}).get("shell_timeout") or 20)
        return self._shell.run(command, timeout=timeout)


def _grab_context() -> str:
    # All text-context modes off → don't read the screen at all; answer from speech.
    if not (config.get("privacy.read_cursor_context", True)
            or config.get("privacy.read_window_context", False)):
        return "(deep context off)"
    try:
        from macos_context import grab_context
        return grab_context()
    except Exception as e:
        _log(f"grab_context failed: {e!r}")
        return "(no context)"


def _grab_screenshot() -> str:
    # Vision context: focused-window JPEG (base64) when the toggle is on, else ''.
    if not config.get("privacy.read_window_screenshot", False):
        return ""
    try:
        from macos_context import grab_window_screenshot
        return grab_window_screenshot()
    except Exception as e:
        _log(f"grab_screenshot failed: {e!r}")
        return ""


def main() -> None:
    import sys
    # Load keys from the workspace .env without importing the GUI module.
    _env = "/Users/robinsverd/Thrivbe-AI/.env"
    if os.path.exists(_env):
        for line in open(_env, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    if "--selftest-shell" in sys.argv:
        sh = Shell()
        sh.run("cd /tmp")
        pwd = sh.run("pwd")
        assert "tmp" in pwd, f"cwd did not persist across commands: {pwd!r}"
        failed = sh.run("false")
        assert "exit 1" in failed, f"exit code not captured: {failed!r}"
        assert "ok" in sh.run("echo ok"), "shell did not recover after a failure"
        sh.close()
        print("SHELL OK:", pwd.strip())
    elif "--selftest-memory" in sys.argv:
        import memory
        marker = "selftest " + uuid.uuid4().hex[:8]
        assert memory.add_learning("fact", marker), "add_learning failed"
        assert marker in memory.top_learnings(50), "remember→learnings round-trip failed"
        print("MEMORY OK")
    elif "--selftest-confirmation" in sys.argv:
        pending = {"args": {"approvalId": 7, "decision": "approve"}, "ts": 100.0}
        assert _is_short_affirm("ja, kjør")
        assert not _is_short_affirm("yes please execute this decision now")
        assert _pending_confirmation_outcome(pending, "yes", now=101.0) == "confirmed"
        assert _pending_confirmation_outcome(pending, "yes no", now=101.0) == "denied"
        assert _pending_confirmation_outcome(pending, "tell me more", now=101.0) == "dropped"
        assert _pending_confirmation_outcome(pending, "yes", now=220.0) == "expired"
        print("CONFIRMATION GATE OK")
    elif "--selftest" in sys.argv:
        reply = asyncio.run(_selftest())
        print("SELFTEST REPLY:", reply)
        assert reply.strip(), "empty reply — protocol/auth problem"
        print("OK")
    else:
        print("Run with --selftest / --selftest-shell / --selftest-memory, "
              "or import LiveSession from agent.py")


if __name__ == "__main__":
    main()
