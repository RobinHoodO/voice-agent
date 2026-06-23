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
import subprocess
import threading
import time
import uuid

_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")   # strip terminal escape codes from shell output

MODEL = "gpt-realtime"   # GA Realtime model (the old beta shape is disabled on this account)
URL = f"wss://api.openai.com/v1/realtime?model={MODEL}"
SR = 24000          # Realtime PCM16 sample rate (fixed by the API)
VOICE = "alloy"     # OpenAI voice — live mode does NOT use ElevenLabs
BLOCK = 2400        # mic frames per callback = 100ms at 24kHz

WORKSPACE = "/Users/robinsverd/Thrivbe-AI"
SKILLS_DIR = f"{WORKSPACE}/skills"
# Cross-session memory: matches *.log → already gitignored; stays in the workspace.
MEMORY = f"{WORKSPACE}/projects/voice-agent/.voice-memory.log"

# Live mode is a proactive, system-wide agentic terminal (looser than push-to-talk's
# SYSTEM in agent.py, which stays narrow). Built into the session instructions.
LIVE_SYSTEM = f"""You are Robin's hands-free voice agent on his Mac. You answer OUT LOUD, so keep replies SHORT and conversational — 1-3 sentences, no markdown, no lists, no emoji.

You have a PERSISTENT shell (run_shell) that starts in Robin's home folder and stays alive for the whole conversation — cd, environment variables, and activated venvs carry between commands. You can act anywhere on the Mac, not just the Thrivbe workspace. Be proactive: when Robin asks you to do something, just do it with run_shell, then say briefly what you did. Don't ask permission for ordinary file/system tasks.

Each turn you also get the UI element under Robin's mouse cursor (role, title, value, selected text) — that's what he's pointing at.

Thrivbe context: the workspace is at {WORKSPACE}; its operating contract is {WORKSPACE}/CLAUDE.md and skills live under {SKILLS_DIR}/<category>/ and ~/.claude/skills/. Read any of these with run_shell when relevant — don't assume, look.

To run a Thrivbe skill or hand off a bigger coding/research task, shell out to: claude -p "<instruction>"  (a full Claude Code agent with every skill and sub-agent). It can take minutes, which would freeze our chat — so for anything slow, background it: claude -p "..." > /tmp/voice-task.txt 2>&1 &  then read /tmp/voice-task.txt when Robin asks how it went.

MEMORY: when Robin tells you a durable fact, preference, or task worth keeping, call the remember tool with a short note. The 'What you remember from before' block below is your memory from past sessions."""

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
        self.p = subprocess.Popen(
            ["/bin/zsh"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
            cwd=os.path.expanduser("~"))
        self.p.stdin.write("source ~/.zshrc 2>/dev/null\n")
        self.p.stdin.flush()
        self._drain(0.6)

    def _drain(self, timeout: float) -> None:
        """Discard buffered startup banner / rc noise."""
        while True:
            r, _, _ = select.select([self.p.stdout], [], [], timeout)
            if not r or self.p.stdout.readline() == "":
                break

    def run(self, cmd: str, timeout: float = 60) -> str:
        if not cmd:
            return "(no command)"
        if self.p.poll() is not None:
            return "error: shell has exited"
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
                return ("".join(lines)[:6000]) + "\n(timed out — still running)"
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
        with open(MEMORY, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M')} | {note}\n")
        return "noted"
    except Exception as e:
        return f"error: {e}"


def _build_live_instructions(ctx: str) -> str:
    """LIVE_SYSTEM + the session's standing context: skill categories, the CLAUDE.md
    pointer (in LIVE_SYSTEM), the memory tail, and what's under the cursor right now."""
    try:
        skills = ", ".join(sorted(os.listdir(SKILLS_DIR)))
    except Exception:
        skills = "(unavailable)"
    blocks = [LIVE_SYSTEM, f"Skill categories available: {skills}"]
    mem = _load_memory_tail()
    if mem:
        blocks.append(f"What you remember from before:\n{mem}")
    if ctx:
        blocks.append(f"What Robin is looking at right now:\n{ctx}")
    return "\n\n".join(blocks)


def _headers() -> dict:
    # GA Realtime: plain bearer auth, no OpenAI-Beta header.
    return {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}


def _log(msg: str) -> None:
    try:
        from agent import LOG
        LOG(msg)
    except Exception:
        pass


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
        instructions = _build_live_instructions(ctx)
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
                        "turn_detection": {"type": "server_vad", "silence_duration_ms": 700,
                                           "create_response": False},
                        "transcription": {"model": "whisper-1"},
                    },
                    "output": {"format": {"type": "audio/pcm", "rate": SR}, "voice": VOICE},
                },
                "tools": TOOLS,
                "tool_choice": "auto",
            },
        }))

    # ----- realtime events -----
    async def _handle(self, ev: dict) -> None:
        t = ev.get("type", "")
        if t in ("session.updated", "input_audio_buffer.speech_started",
                 "input_audio_buffer.speech_stopped", "response.created",
                 "response.done", "error"):
            _log(f"ev {t}" + (f" {ev.get('error')}" if t == "error" else ""))
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
        return self._shell.run(command)

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
        try:
            dev = sd.query_devices(kind="input")
            _log(f"audio input device: {dev.get('name')!r} default_sr={dev.get('default_samplerate')}")
        except Exception as e:
            _log(f"query input device failed: {e!r}")
        self._in_stream = sd.RawInputStream(
            samplerate=SR, channels=1, dtype="int16",
            blocksize=BLOCK, callback=self._mic_cb)
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
        try:
            self._out_stream = sd.RawOutputStream(samplerate=SR, channels=1, dtype="int16")
            self._out_stream.start()
        except Exception as e:
            _log(f"player init failed: {e!r}")
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
