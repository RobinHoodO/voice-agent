"""LiveSession — one live Realtime conversation: orchestration only.

The split-out collaborators live in: realtime_client.py (protocol/constants/auth),
audio.py (AudioMixin: mic/playback), shell.py (Shell), tools.py (TOOLS + helpers),
live_prompt.py (instructions), macos_context.py (screen capture). This module wires
them together and runs the asyncio event loop + the OpenAI Realtime event handler.
"""
import asyncio
import base64
import json
import os
import queue
import subprocess
import threading
import time
import uuid

import config
from audio import AudioMixin
from live_prompt import _build_live_instructions
from realtime_client import SR, URL, VOICE, _headers, _selftest
from shell import Shell
from tools import TOOLS, _build_delegate_cmd, _extract_json, _put_text


def _log(msg: str) -> None:
    try:
        from agent import LOG
        LOG(msg)
    except Exception:
        pass


class LiveSession(AudioMixin):
    """One live Realtime conversation. start()/stop() are called from the main
    (rumps) thread; everything else runs on the session's own asyncio thread.
    Audio I/O comes from AudioMixin."""

    def __init__(self, on_state=None, announce=None, on_auto_stop=None):
        self.on_state = on_state or (lambda s: None)
        self._announce = announce          # if set, speak this aloud right after opening
        self._on_auto_stop = on_auto_stop  # called when the idle/max watchdog ends the session
        self._session_start = 0.0          # loop.time() when this session opened
        self._last_speech = 0.0            # loop.time() of the last detected speech turn
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ws = None
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
        self._turns: list = []                        # ["you: …", "agent: …"] for this conversation
        self.level: float = 0.0                       # live mic level 0..1 (drives the wave pill)
        self._cfg: dict = {}                          # snapshot of config for this session

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

    def _persist_conversation(self) -> None:
        """On close, store this conversation's transcript, then summarize it in the
        background (non-blocking) so future sessions can recall it. Best-effort."""
        turns = self._turns
        if not turns:
            return
        transcript = "\n".join(turns)
        try:
            import memory
            cid = memory.record(transcript)
            _log(f"conversation stored (id={cid}, {len(turns)} turns)")
            if cid:
                threading.Thread(target=self._learn, args=(cid, transcript),
                                 daemon=True).start()
        except Exception as e:
            _log(f"persist conversation failed: {e!r}")

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
        import websockets
        self._mic_q = asyncio.Queue()
        async with websockets.connect(URL, additional_headers=_headers(),
                                      max_size=None) as ws:
            self._ws = ws
            self._cfg = config.load()
            try:
                self._shell = Shell()
            except Exception as e:
                _log(f"persistent shell start failed: {e!r}")
            await self._configure(ws)
            self._start_audio()
            self._mic_task = asyncio.ensure_future(self._pump_mic())
            self._mic_task.add_done_callback(self._on_mic_task_done)
            self.on_state("listening")
            _log("realtime session open — listening")
            self._session_start = self._last_speech = self._loop.time()
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

    async def _configure(self, ws) -> None:
        ctx = await self._loop.run_in_executor(None, _grab_context)
        live = self._cfg.get("live") or {}
        instructions = _build_live_instructions(ctx, self._cfg)
        voice = live.get("voice") or VOICE
        # Respect the agentic-shell toggle. Fail CLOSED — default False to match
        # config.DEFAULTS and the menu, so a missing key never exposes the shell.
        # run_shell + delegate both act on the system, so both are gated; put_text
        # (clipboard/paste) and remember stay available either way.
        tools = TOOLS if live.get("agentic_shell", False) else [
            t for t in TOOLS if t.get("name") not in ("run_shell", "delegate")]
        await ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "type": "realtime",
                "output_modalities": ["audio"],
                "instructions": instructions,
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": SR},
                        # create_response:false so we can inject fresh cursor context
                        # AFTER the user stops talking, then trigger the response ourselves.
                        # threshold raised (default 0.5) so a sensitive/bone-conduction mic
                        # doesn't false-trigger barge-in and cancel replies mid-sentence.
                        "turn_detection": {"type": "server_vad", "threshold": 0.6,
                                           "prefix_padding_ms": 300, "silence_duration_ms": 700,
                                           "create_response": False},
                        "transcription": {"model": "whisper-1"},
                    },
                    "output": {"format": {"type": "audio/pcm", "rate": SR}, "voice": voice},
                },
                "tools": tools,
                "tool_choice": "auto",
            },
        }))

    # ----- realtime events -----
    async def _handle(self, ev: dict) -> None:
        t = ev.get("type", "")
        if t in ("session.updated", "input_audio_buffer.speech_started",
                 "input_audio_buffer.speech_stopped", "response.created",
                 "error"):
            _log(f"ev {t}" + (f" {ev.get('error')}" if t == "error" else ""))
        if t == "response.created":
            self._audio_n = 0
        elif t == "response.done":
            resp = ev.get("response") or {}
            status = resp.get("status")
            details = resp.get("status_details")
            _log(f"ev response.done status={status} audio_deltas={getattr(self, '_audio_n', 0)} "
                 f"details={details}")
        if t in ("input_audio_buffer.speech_started", "input_audio_buffer.speech_stopped"):
            self._last_speech = self._loop.time()   # reset the idle watchdog on any turn
        if t == "input_audio_buffer.speech_started":
            self._flush_out()                       # barge-in: stop talking
            if self._speaking:
                await self._ws.send(json.dumps({"type": "response.cancel"}))
                self._speaking = False
            self.on_state("listening")
        elif t == "input_audio_buffer.speech_stopped":
            await self._inject_context_and_respond()
        elif t == "response.output_audio.delta":
            self._enqueue_audio(base64.b64decode(ev["delta"]))
            self._audio_n = getattr(self, "_audio_n", 0) + 1
            if self._audio_n == 1:
                _log("first audio delta -> speaking")
            if not self._speaking:
                self._speaking = True
                self.on_state("speaking")
        elif t == "response.output_audio.done":
            self._speaking = False
            self.on_state("listening")
        elif t == "response.output_item.added":
            # function_call items carry the tool name + call_id; remember it so
            # _do_tool can route (the .arguments.done event may omit the name).
            item = ev.get("item") or {}
            if item.get("type") == "function_call" and item.get("call_id"):
                self._fn_names[item["call_id"]] = item.get("name", "")
        elif t == "response.function_call_arguments.done":
            await self._do_tool(ev)
        elif t == "conversation.item.input_audio_transcription.completed":
            heard = ev.get("transcript", "").strip()
            _log(f"live heard: {heard!r}")
            if heard:
                config.activity(f"🗣  you: {heard}")
                self._turns.append(f"you: {heard}")
        elif t == "response.output_audio_transcript.done":
            said = (ev.get("transcript") or "").strip()
            if said:
                config.activity(f"💬  agent: {said}")
                self._turns.append(f"agent: {said}")
        elif t == "error":
            _log(f"realtime error event: {ev.get('error')}")

    async def _speak_announcement(self) -> None:
        """Auto-wake greeting: the menubar watcher opened this session because a
        background task finished. Tell the model to report the result out loud."""
        # Keep the TAIL, not the head: the VERIFIED/UNVERIFIED/FAILED tag is the last line,
        # so truncating from the front would drop the very verdict we want spoken.
        txt = (self._announce or "")[-2500:]
        self._announce = None
        config.activity("🔔  task finished — speaking result")
        await self._ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {"type": "message", "role": "user",
                     "content": [{"type": "input_text",
                                  "text": ("[A background task you started earlier just finished. "
                                           "Greet me briefly and tell me out loud, in 1-3 sentences, "
                                           "what it found. The result ends with a status tag — "
                                           "VERIFIED means it confirmed the end-state (say it's done); "
                                           "UNVERIFIED means it could NOT confirm it (say so plainly — "
                                           "tell me what couldn't be confirmed, don't imply success); "
                                           "FAILED means it didn't work. Be honest about which it is. "
                                           "Result:\n" + txt + "\n]")}]},
        }))
        await self._ws.send(json.dumps({"type": "response.create"}))

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
        if shot:
            # Separate item so a rejected image never blocks the text context.
            await self._ws.send(json.dumps({
                "type": "conversation.item.create",
                "item": {"type": "message", "role": "user",
                         "content": [{"type": "input_image",
                                      "image_url": f"data:image/jpeg;base64,{shot}"}]},
            }))
        await self._ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {"type": "message", "role": "user",
                     "content": [{"type": "input_text",
                                  "text": f"[What I'm looking at right now:\n{ctx}\n"
                                          + ("(A screenshot of my screen is attached above.)\n" if shot else "")
                                          + "]"}]},
        }))
        await self._ws.send(json.dumps({"type": "response.create"}))

    async def _do_tool(self, ev: dict) -> None:
        self.on_state("acting")          # running a tool/command — distinct from thinking
        call_id = ev.get("call_id")
        name = ev.get("name") or self._fn_names.pop(call_id, "") or "run_shell"
        try:
            args = json.loads(ev.get("arguments") or "{}")
        except Exception:
            args = {}
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
            instr = args.get("instruction", "")
            try:
                import memory
                mem_ctx = memory.recall(instr, k=3)
            except Exception:
                mem_ctx = ""
            if mem_ctx:
                instr = (f"Relevant context from prior conversations/memory:\n{mem_ctx}\n\n"
                         f"Task:\n{instr}\n\n"
                         f"(You can also search memory yourself: run "
                         f"`python {os.path.join(os.path.dirname(os.path.abspath(__file__)), 'memory.py')} "
                         f"recall \"<query>\"`.)")
            built = _build_delegate_cmd(instr, self._cfg)
            if not built:
                out = "couldn't start it (delegation is off or the instruction was empty)"
            else:
                cmd, _out_path = built
                self._run_in_shell(cmd)   # returns instantly; runs detached or in a Terminal
                if (self._cfg.get("live") or {}).get("show_task_terminals"):
                    out = "Opened it in a Terminal so you can watch it work."
                else:
                    out = "Started it in the background — I'll come back with the result when it's done."
            config.activity(f"🚀  delegated: {args.get('instruction', '')[:80]}")
        else:
            out = await self._loop.run_in_executor(None, self._run_in_shell, args.get("command", ""))
        await self._ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {"type": "function_call_output", "call_id": call_id, "output": out},
        }))
        await self._ws.send(json.dumps({"type": "response.create"}))

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
