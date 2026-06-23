#!/usr/bin/env python3
"""Live conversation mode — OpenAI Realtime API (speech-to-speech).

Runs an async WebSocket session on its own daemon thread so the rumps/pynput
main loop never blocks. Streams mic PCM16 in, plays the model's PCM16 out,
supports barge-in (interrupt mid-reply), runs the `run_shell` tool, and
refreshes cursor/selection context each turn. agent.py starts/stops it.

Standalone check (no GUI, text only):  .venv/bin/python realtime.py --selftest

Agent helpers (LOG, grab_context, run_shell, SYSTEM) are imported lazily inside
methods, not at module top — this breaks the agent<->realtime import cycle and
keeps --selftest from pulling in rumps/ffmpeg.
"""
import asyncio
import base64
import json
import os
import queue
import re
import select
import signal
import subprocess
import threading
import time
import uuid

import config

_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")   # strip terminal escape codes from shell output

MODEL = "gpt-realtime"   # GA Realtime model (the old beta shape is disabled on this account)
URL = f"wss://api.openai.com/v1/realtime?model={MODEL}"
SR = 24000          # Realtime PCM16 sample rate (fixed by the API)
VOICE = "alloy"     # default OpenAI voice (overridable via config live.voice)
BLOCK = 2400        # mic frames per callback = 100ms at 24kHz
# ponytail: pin live audio by device-name substring. Using a BT headset as the MIC
# forces macOS into low-quality HFP/call mode (quiet playback), so by default capture
# from the built-in mic and play to a headset if present. config.audio overrides these.
MIC_NAME = "MacBook"      # default input: built-in MacBook mic
OUT_NAME = "OpenComm"     # default output preference: Shokz headset (else system default)

MEMORY = config.MEMORY_PATH   # per-user, out of any project folder

# Live mode is a proactive, system-wide agentic terminal. The workspace/delegation
# blocks are added per-session from config so it isn't hardwired to one user's setup.
LIVE_SYSTEM = """You are a hands-free voice agent running on the user's Mac. You answer OUT LOUD, so keep replies SHORT and conversational — 1-3 sentences, no markdown, no lists, no emoji.

You have a PERSISTENT shell (run_shell) that starts in the user's home folder and stays alive for the whole conversation — cd, environment variables, and activated venvs carry between commands. You can act anywhere on the Mac. Be proactive: when asked to do something, just do it with run_shell, then say briefly what you did. Don't ask permission for ordinary file/system tasks.

SPEED MATTERS — commands run while the user waits in silence, and anything that runs too long is killed. To find files or folders use `mdfind` (Spotlight, instant), e.g. `mdfind -name report`. NEVER run a recursive `find ~`, `find /`, or `ls -R ~` — they scan the whole disk and time out.

Each turn you also get the UI element under the user's mouse cursor (role, title, value, selected text) — that's what they're pointing at.

MEMORY: when the user tells you a durable fact, preference, or task worth keeping, call the remember tool with a short note. The 'What you remember from before' block below is your memory from past sessions."""


def _delegation_line(cfg: dict) -> str:
    live = cfg.get("live") or {}
    mode = live.get("delegate", "pi")
    if mode == "off":
        return ""
    cmd = ('claude -p "<instruction>"' if mode == "claude"
           else 'pi -p --model %s "<instruction>"' % live.get("pi_model", "deepseek-v4-flash"))
    return ("To run a skill or hand off a bigger coding/research task, shell out to: " + cmd +
            " (a headless AI agent with file/bash tools). It can take a while and would freeze "
            "this chat, so background slow jobs with `> /tmp/voice-task.txt 2>&1 &` and read that "
            "file when asked how it went.")

# Realtime tool schema is flat (name/parameters at top level), unlike the
# chat-completions nested {"function": {...}} shape in agent.py.
TOOLS = [
    {
        "type": "function",
        "name": "run_shell",
        "description": "Run a command in the persistent shell (starts in Robin's home folder; cd/env persist; can act anywhere on the Mac). Use it to read, search, act, or delegate via `claude -p`.",
        "parameters": {"type": "object",
                       "properties": {"command": {"type": "string"}},
                       "required": ["command"]},
    },
    {
        "type": "function",
        "name": "remember",
        "description": "Save a short durable note (a fact, preference, or task) so you recall it in future sessions.",
        "parameters": {"type": "object",
                       "properties": {"note": {"type": "string"}},
                       "required": ["note"]},
    },
]


class Shell:
    """One persistent zsh for a live session. cd/env/venvs persist across .run() calls;
    starts in the home folder so the agent is system-wide. Non-interactive (so no prompt
    junk pollutes captured output) but sources ~/.zshrc, so Robin's PATH and shell
    functions (incl. the `claude` wrapper) are available.

    ponytail: sentinel-delimited capture on one shell. A command that never returns
    (opens a REPL) desyncs it — rare; restart live mode to reset. Long jobs: background
    them (`cmd &`) and poll — the prompt tells the model to.
    """

    def __init__(self) -> None:
        self._spawn()

    def _spawn(self) -> None:
        # start_new_session=True puts the shell + its children in their own process
        # group, so a runaway command can be killed wholesale on timeout (_respawn).
        self.p = subprocess.Popen(
            ["/bin/zsh"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
            cwd=os.path.expanduser("~"), start_new_session=True)
        self.p.stdin.write("source ~/.zshrc 2>/dev/null\n")
        self.p.stdin.flush()
        self._drain(0.6)

    def _respawn(self) -> None:
        """A timed-out command leaves the shell blocked (zsh runs input serially), so
        the next command would queue behind it and desync. Nuke the whole process group
        (kills the runaway child too) and start fresh. Cost: cwd/env reset to home."""
        try:
            os.killpg(os.getpgid(self.p.pid), signal.SIGKILL)
        except Exception:
            pass
        self._spawn()

    def _drain(self, timeout: float) -> None:
        """Discard buffered startup banner / rc noise."""
        while True:
            r, _, _ = select.select([self.p.stdout], [], [], timeout)
            if not r or self.p.stdout.readline() == "":
                break

    def run(self, cmd: str, timeout: float = 20) -> str:
        if not cmd:
            return "(no command)"
        if self.p.poll() is not None:
            self._respawn()
        mark = f"__VA_{uuid.uuid4().hex}__"
        try:
            self.p.stdin.write(f'{cmd}\nprint -r -- "{mark}$?"\n')
            self.p.stdin.flush()
        except Exception as e:
            return f"error: {e}"
        lines, deadline = [], time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._respawn()   # kill the wedged command, reset to a clean shell
                partial = "".join(lines)[:4000]
                return (partial + f"\n(timed out after {int(timeout)}s and was killed. "
                        "Use a faster command — e.g. `mdfind` for files, not `find ~`.)")
            r, _, _ = select.select([self.p.stdout], [], [], remaining)
            if not r:
                continue
            line = self.p.stdout.readline()
            if line == "":
                return "".join(lines)[:6000] or "(shell closed)"
            clean = _ANSI.sub("", line)
            if mark in clean:
                code = clean.split(mark, 1)[1].strip()
                out = "".join(lines)
                if code not in ("0", ""):
                    out += f"\n(exit {code})"
                return out[:6000] or "(no output)"
            lines.append(clean)

    def close(self) -> None:
        # Kill the whole process group, not just the shell — otherwise a backgrounded
        # child (e.g. a `pi ... &` delegation) orphans and keeps running after the
        # session ends. Ending a live session must reclaim everything it spawned.
        try:
            os.killpg(os.getpgid(self.p.pid), signal.SIGKILL)
        except Exception:
            try:
                self.p.terminate()
            except Exception:
                pass


def _load_memory_tail(n: int = 30) -> str:
    try:
        with open(MEMORY, encoding="utf-8") as f:
            return "".join(f.readlines()[-n:]).strip()
    except FileNotFoundError:
        return ""
    except Exception as e:
        _log(f"memory read failed: {e!r}")
        return ""


def _remember(note: str) -> str:
    note = (note or "").strip()
    if not note:
        return "nothing to remember"
    try:
        config.ensure_dirs()
        with open(MEMORY, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M')} | {note}\n")
        return "noted"
    except Exception as e:
        return f"error: {e}"


def _build_live_instructions(ctx: str, cfg: dict | None = None) -> str:
    """LIVE_SYSTEM + per-session context from config: optional workspace + its skills,
    the delegation line, the memory tail, and what's under the cursor right now."""
    cfg = cfg or config.load()
    blocks = [LIVE_SYSTEM]
    ws = (cfg.get("live") or {}).get("workspace")
    if ws:
        ws = os.path.expanduser(ws)
        blocks.append(f"Workspace: {ws} — its operating contract may be {ws}/CLAUDE.md. "
                      f"Read files there with run_shell when relevant.")
        try:
            cats = ", ".join(sorted(os.listdir(os.path.join(ws, "skills"))))
            blocks.append(f"Skill categories in the workspace: {cats}")
        except Exception:
            pass
    deleg = _delegation_line(cfg)
    if deleg:
        blocks.append(deleg)
    mem = _load_memory_tail()
    if mem:
        blocks.append(f"What you remember from before:\n{mem}")
    if ctx:
        blocks.append(f"What the user is looking at right now:\n{ctx}")
    return "\n\n".join(blocks)


def _headers() -> dict:
    # GA Realtime: plain bearer auth, no OpenAI-Beta header. Key from Keychain
    # (config), falling back to env in dev.
    return {"Authorization": f"Bearer {config.secret('openai') or ''}"}


def _log(msg: str) -> None:
    try:
        from agent import LOG
        LOG(msg)
    except Exception:
        pass


def _find_device(substr: str, want_input: bool):
    """Index of the first device whose name contains substr (case-insensitive) and has
    the right direction; None (= system default) if not found."""
    import sounddevice as sd
    try:
        for i, d in enumerate(sd.query_devices()):
            ch = d["max_input_channels"] if want_input else d["max_output_channels"]
            if ch > 0 and substr.lower() in d["name"].lower():
                return i
    except Exception as e:
        _log(f"device lookup failed: {e!r}")
    return None


class LiveSession:
    """One live Realtime conversation. start()/stop() are called from the main
    (rumps) thread; everything else runs on the session's own asyncio thread."""

    def __init__(self, on_state=None):
        self.on_state = on_state or (lambda s: None)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ws = None
        self._mic_q: asyncio.Queue | None = None      # mic bytes -> ws (loop thread)
        self._out_q: queue.Queue = queue.Queue()      # model bytes -> speaker (player thread)
        self._in_stream = None
        self._out_stream = None
        self._player_thread: threading.Thread | None = None
        self._running = False
        self._speaking = False
        self._shell: Shell | None = None             # persistent zsh for this session
        self._fn_names: dict = {}                     # call_id -> tool name (from output_item.added)
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

    # ----- session thread -----
    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._session())
        except Exception as e:
            _log(f"realtime session error: {e!r}")
        finally:
            self._teardown_audio()
            self._running = False
            self.on_state("idle")
            _log("realtime session closed")

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
            asyncio.ensure_future(self._pump_mic())
            self.on_state("listening")
            _log("realtime session open — listening")
            async for raw in ws:
                if not self._running:
                    break
                try:
                    await self._handle(json.loads(raw))
                except Exception as e:
                    _log(f"realtime handle error: {e!r}")

    async def _configure(self, ws) -> None:
        ctx = await self._loop.run_in_executor(None, _grab_context)
        live = self._cfg.get("live") or {}
        instructions = _build_live_instructions(ctx, self._cfg)
        voice = live.get("voice") or VOICE
        # Respect the agentic-shell toggle (off-by-default is the product safety default).
        tools = TOOLS if live.get("agentic_shell", True) else [
            t for t in TOOLS if t.get("name") != "run_shell"]
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
        if t == "input_audio_buffer.speech_started":
            self._flush_out()                       # barge-in: stop talking
            if self._speaking:
                await self._ws.send(json.dumps({"type": "response.cancel"}))
                self._speaking = False
            self.on_state("listening")
        elif t == "input_audio_buffer.speech_stopped":
            await self._inject_context_and_respond()
        elif t == "response.output_audio.delta":
            self._out_q.put(base64.b64decode(ev["delta"]))
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
            _log(f"live heard: {ev.get('transcript', '')!r}")
        elif t == "error":
            _log(f"realtime error event: {ev.get('error')}")

    async def _inject_context_and_respond(self) -> None:
        self.on_state("thinking")
        ctx = await self._loop.run_in_executor(None, _grab_context)
        _log(f"context injected ({len(ctx)} chars): {ctx[:160]!r}")
        await self._ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {"type": "message", "role": "user",
                     "content": [{"type": "input_text",
                                  "text": f"[What I'm looking at right now:\n{ctx}\n]"}]},
        }))
        await self._ws.send(json.dumps({"type": "response.create"}))

    async def _do_tool(self, ev: dict) -> None:
        self.on_state("thinking")
        call_id = ev.get("call_id")
        name = ev.get("name") or self._fn_names.pop(call_id, "") or "run_shell"
        try:
            args = json.loads(ev.get("arguments") or "{}")
        except Exception:
            args = {}
        if name == "remember":
            out = _remember(args.get("note", ""))
            _log(f"remember: {args.get('note', '')!r}")
        else:
            out = await self._loop.run_in_executor(None, self._run_in_shell, args.get("command", ""))
        await self._ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {"type": "function_call_output", "call_id": call_id, "output": out},
        }))
        await self._ws.send(json.dumps({"type": "response.create"}))

    def _run_in_shell(self, command: str) -> str:
        if self._shell is None:
            return "error: shell not started"
        _log(f"live $ {command}")
        timeout = float((self._cfg.get("live") or {}).get("shell_timeout") or 20)
        return self._shell.run(command, timeout=timeout)

    async def _pump_mic(self) -> None:
        n = 0
        while self._running:
            try:
                data = await self._mic_q.get()
            except Exception:
                break
            if data is None or self._ws is None:
                continue
            try:
                await self._ws.send(json.dumps({
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(data).decode(),
                }))
                n += 1
                if n == 1 or n % 50 == 0:
                    _log(f"mic frames sent: {n}")
            except Exception as e:
                _log(f"mic pump send failed: {e!r}")
                break

    # ----- audio -----
    def _start_audio(self) -> None:
        import sounddevice as sd
        want_mic = (self._cfg.get("audio") or {}).get("input_device") or MIC_NAME
        mic = _find_device(want_mic, want_input=True)
        try:
            name = sd.query_devices(mic if mic is not None else None, kind="input").get("name")
            _log(f"audio input device: {name!r} (idx={mic}, pinned to {want_mic!r})")
        except Exception as e:
            _log(f"query input device failed: {e!r}")
        self._in_stream = sd.RawInputStream(
            samplerate=SR, channels=1, dtype="int16",
            blocksize=BLOCK, callback=self._mic_cb, device=mic)
        self._in_stream.start()
        _log("audio input stream started")
        self._player_thread = threading.Thread(target=self._player, daemon=True)
        self._player_thread.start()

    def _mic_cb(self, indata, frames, t, status) -> None:
        # PortAudio thread — hand bytes to the asyncio loop without blocking.
        if self._loop and self._running and self._mic_q is not None:
            data = bytes(indata)
            try:
                self._loop.call_soon_threadsafe(self._mic_q.put_nowait, data)
            except Exception:
                pass

    def _player(self) -> None:
        import sounddevice as sd
        want_out = (self._cfg.get("audio") or {}).get("output_device") or OUT_NAME
        out = _find_device(want_out, want_input=False)
        try:
            name = sd.query_devices(out if out is not None else None, kind="output").get("name")
            _log(f"audio output device: {name!r} (idx={out}, prefer {want_out!r})")
        except Exception as e:
            _log(f"query output device failed: {e!r}")
        try:
            self._out_stream = sd.RawOutputStream(samplerate=SR, channels=1, dtype="int16", device=out)
            self._out_stream.start()
        except Exception as e:
            _log(f"player init failed: {e!r} — falling back to default output")
            try:
                self._out_stream = sd.RawOutputStream(samplerate=SR, channels=1, dtype="int16")
                self._out_stream.start()
            except Exception as e2:
                _log(f"player default init also failed: {e2!r}")
                return
        while self._running:
            try:
                chunk = self._out_q.get(timeout=0.1)
            except queue.Empty:
                continue
            if not chunk:
                continue
            try:
                self._out_stream.write(chunk)
            except Exception as e:
                _log(f"player write: {e!r}")

    def _flush_out(self) -> None:
        # Barge-in: drop queued audio. The ~100ms chunk mid-write finishes — close enough.
        try:
            while True:
                self._out_q.get_nowait()
        except queue.Empty:
            pass

    def _teardown_audio(self) -> None:
        for s in (self._in_stream, self._out_stream):
            try:
                if s:
                    s.stop()
                    s.close()
            except Exception:
                pass
        self._in_stream = self._out_stream = None
        if self._shell is not None:
            self._shell.close()
            self._shell = None


def _grab_context() -> str:
    try:
        from agent import grab_context
        return grab_context()
    except Exception as e:
        _log(f"grab_context failed: {e!r}")
        return "(no context)"


async def _selftest() -> str:
    """Text-only round-trip — proves auth + model + protocol with no audio/GUI."""
    import websockets
    async with websockets.connect(URL, additional_headers=_headers(), max_size=None) as ws:
        await ws.send(json.dumps({"type": "session.update",
                                  "session": {"type": "realtime", "output_modalities": ["text"],
                                              "instructions": "Reply in one short sentence."}}))
        await ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "Say hello to Robin."}]}}))
        await ws.send(json.dumps({"type": "response.create"}))
        text = ""
        async for raw in ws:
            ev = json.loads(raw)
            tp = ev.get("type", "")
            if tp == "response.output_text.delta":
                text += ev.get("delta", "")
            elif tp == "response.output_text.done":
                text = ev.get("text", text)
            elif tp == "response.done":
                break
            elif tp == "error":
                raise RuntimeError(ev.get("error"))
        return text


if __name__ == "__main__":
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
        marker = "selftest " + uuid.uuid4().hex[:8]
        assert _remember(marker) == "noted"
        assert marker in _load_memory_tail(), "remember/tail round-trip failed"
        print("MEMORY OK")
    elif "--selftest" in sys.argv:
        reply = asyncio.run(_selftest())
        print("SELFTEST REPLY:", reply)
        assert reply.strip(), "empty reply — protocol/auth problem"
        print("OK")
    else:
        print("Run with --selftest / --selftest-shell / --selftest-memory, "
              "or import LiveSession from agent.py")
