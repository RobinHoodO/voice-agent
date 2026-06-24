#!/usr/bin/env python3
"""Live conversation mode — OpenAI Realtime API (speech-to-speech).

Runs an async WebSocket session on its own daemon thread so the rumps/pynput
main loop never blocks. Streams mic PCM16 in, plays the model's PCM16 out,
supports barge-in (interrupt mid-reply), runs the `run_shell` tool, and
refreshes cursor/selection context each turn. agent.py starts/stops it.

Standalone check (no GUI, text only):  .venv/bin/python realtime.py --selftest

Agent helpers (LOG, run_shell, SYSTEM) are imported lazily inside methods, not at
module top; screen context comes from macos_context. This breaks the agent<->realtime cycle and
keeps --selftest from pulling in rumps/ffmpeg.
"""
import asyncio
import base64
import json
import os
import queue
import re
import select
import shlex
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

You have a PERSISTENT shell (run_shell) that starts in the user's configured base folder (their workspace) and stays alive for the whole conversation — cd, environment variables, and activated venvs carry between commands. You can act anywhere on the Mac. Be proactive: when asked to do something, just do it with run_shell, then say briefly what you did. Don't ask permission for ordinary file/system tasks.

SPEED MATTERS — commands run while the user waits in silence, and anything that runs too long is killed. To find files or folders use `mdfind` (Spotlight, instant), e.g. `mdfind -name report`. NEVER run a recursive `find ~`, `find /`, or `ls -R ~` — they scan the whole disk and time out.

Each turn you may also receive on-screen context (whatever the user has enabled in Settings): the text under their mouse cursor and any marked selection, the full text of the window they're in, and/or a screenshot of that window. Use whatever arrives as what they're looking at right now. Describe your sight by what you actually got this turn — if a screenshot or the full window text is attached, you can genuinely see the window, so don't claim you only see the cursor.

PUTTING TEXT IN A WINDOW: you CAN type/paste into whatever app the user is in — call put_text with the exact text. It copies to the clipboard and pastes into the frontmost window. Use it whenever the user asks you to write, insert, or paste something into an email, doc, or field. Never claim you can't reach the clipboard or the window.

DELEGATING SLOW WORK: for a coding/research job too slow to do inline, call delegate with a clear instruction. It runs in the background, survives this conversation, and when it finishes the voice agent automatically comes back and speaks the result — so launch it and move on, don't wait or poll.

YOUR OWN BRIEFING: you have persistent custom instructions (shown below if set) that reload every session. When the user wants to set up or refine how you work — your persona, who they are, what their workspace is for — interview them briefly, and feel free to delegate a task to explore their machine/workspace for relevant context, then call set_prompt to save a tight briefing for your future self.

MEMORY: you ALREADY remember every conversation and continuously learn the user's preferences and facts on your own — the 'What I've learned about you' and recent-conversation blocks below are that memory, kept up to date automatically. Don't re-note something you just recalled, and don't describe yourself as merely "storing notes" — you learn and self-correct over time. Use the remember tool only when the user gives you an explicit, durable fact to keep right now; use recall to search deeper."""


def _delegation_line(cfg: dict) -> str:
    """One sentence naming the background agent behind the `delegate` tool, or '' if off."""
    live = cfg.get("live") or {}
    mode = live.get("delegate", "pi")
    if mode == "off":
        return ""
    who = "claude" if mode == "claude" else f"pi ({live.get('pi_model', 'deepseek-v4-flash')})"
    return f"The delegate tool hands work to {who}, a headless AI agent with file/bash tools."


def _build_delegate_cmd(instruction: str, cfg: dict) -> str | None:
    """Write the instruction to a prompt file and return a shell command that runs the
    background agent DETACHED, capturing output to TASKS_DIR/<id>.out and dropping a
    .done sentinel on completion (the menubar watcher polls for it to auto-wake and
    speak the result). Returns None if delegation is off or the instruction is empty."""
    instruction = (instruction or "").strip()
    live = cfg.get("live") or {}
    mode = live.get("delegate", "pi")
    if not instruction or mode == "off":
        return None
    if mode == "claude":
        agent_cmd = "claude -p"
    else:
        agent_cmd = f"pi -p --model {live.get('pi_model', 'deepseek-v4-flash')}"
    try:
        config.ensure_dirs()
        tid = time.strftime("%H%M%S")
        pf = os.path.join(config.TASKS_DIR, f"{tid}.prompt")
        out = os.path.join(config.TASKS_DIR, f"{tid}.out")
        done = os.path.join(config.TASKS_DIR, f"{tid}.done")
        with open(pf, "w", encoding="utf-8") as f:
            f.write(instruction)
        # $(cat prompt) avoids any shell-injection from the instruction text itself.
        # </dev/null is essential: detached under the live shell, the agent would
        # otherwise inherit an open stdin that never EOFs and block forever (0% CPU,
        # no output, no .done — so the auto-wake never fires).
        runner = (f'{agent_cmd} "$(cat {shlex.quote(pf)})" </dev/null > {shlex.quote(out)} 2>&1; '
                  f'touch {shlex.quote(done)}')
        # Return the out-path too so the caller can open a live log window on it.
        return (f"nohup sh -c {shlex.quote(runner)} >/dev/null 2>&1 & disown", out)
    except Exception as e:
        _log(f"delegate build failed: {e!r}")
        return None


def _open_task_log(out_path: str) -> None:
    """Open a Terminal window that tails a delegated task's output, so the user can
    watch the background pi job live. Best-effort; any failure is non-fatal.
    Note: `pi -p` block-buffers to a file, so output often lands in one burst near
    completion rather than streaming line-by-line — the window still surfaces it."""
    try:
        p = out_path.replace('"', '\\"')
        script = ('tell application "Terminal"\n'
                  ' activate\n'
                  f' do script "clear; echo \\"⏳ Thrivbe Voice — background task (pi). Live output:\\"; '
                  f'echo; tail -n +1 -F \\"{p}\\""\n'
                  'end tell')
        subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    except Exception as e:
        _log(f"open task log failed: {e!r}")

# Realtime tool schema is flat (name/parameters at top level), unlike the
# chat-completions nested {"function": {...}} shape in agent.py.
TOOLS = [
    {
        "type": "function",
        "name": "run_shell",
        "description": "Run a command in the persistent shell (starts in Robin's configured base folder; cd/env persist; can act anywhere on the Mac). Use it to read, search, and act. To hand off a slow coding/research task, ALWAYS use the `delegate` tool (it runs `pi` in the background and auto-wakes with the result) — do NOT shell out to `claude` or `pi` yourself.",
        "parameters": {"type": "object",
                       "properties": {"command": {"type": "string"}},
                       "required": ["command"]},
    },
    {
        "type": "function",
        "name": "remember",
        "description": "Explicitly save one durable fact or preference about the user into your long-term memory (you already learn most things automatically — use this only when the user clearly wants something kept). Don't use it to re-store something you just recalled.",
        "parameters": {"type": "object",
                       "properties": {"note": {"type": "string"}},
                       "required": ["note"]},
    },
    {
        "type": "function",
        "name": "recall",
        "description": "Search your memory — past conversations and the user's wider knowledge — for relevant context. Use it when the user refers to something from before, or when you need background you don't already have in this conversation.",
        "parameters": {"type": "object",
                       "properties": {"query": {"type": "string"}},
                       "required": ["query"]},
    },
    {
        "type": "function",
        "name": "put_text",
        "description": "Put text into the app the user is in: copies it to the clipboard and (by default) pastes it into the frontmost window with Cmd-V. Use for writing/inserting/pasting into emails, docs, fields.",
        "parameters": {"type": "object",
                       "properties": {"text": {"type": "string"},
                                      "paste": {"type": "boolean",
                                                "description": "true (default) = paste into the front window; false = only copy to clipboard"}},
                       "required": ["text"]},
    },
    {
        "type": "function",
        "name": "delegate",
        "description": "Hand a slow coding/research task to a background AI agent. Returns immediately and survives the conversation; when it finishes the voice agent automatically comes back and speaks the result. Don't wait or poll.",
        "parameters": {"type": "object",
                       "properties": {"instruction": {"type": "string"}},
                       "required": ["instruction"]},
    },
    {
        "type": "function",
        "name": "set_prompt",
        "description": "Save (overwrite) your persistent custom instructions — your persona and standing context about the user and their workspace. Reloaded at the start of every future conversation. Use when the user asks you to remember how to behave, or after researching their setup to write your own briefing.",
        "parameters": {"type": "object",
                       "properties": {"text": {"type": "string"}},
                       "required": ["text"]},
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
        # Start in the configured base folder (live.workspace) so the agent's
        # project skills and files are in reach; fall back to home if unset/missing.
        ws = config.get("live.workspace")
        start = os.path.expanduser(ws) if ws else os.path.expanduser("~")
        if not os.path.isdir(start):
            start = os.path.expanduser("~")
        # start_new_session=True puts the shell + its children in their own process
        # group, so a runaway command can be killed wholesale on timeout (_respawn).
        self.p = subprocess.Popen(
            ["/bin/zsh"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
            cwd=start, start_new_session=True,
            env=config.subprocess_env())   # strip API keys: model-run commands must not read them
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
        # End the interactive shell only — terminate the zsh, NOT its process group.
        # Jobs the agent launched detached (`nohup ... & disown`) are reparented to
        # launchd and keep running after the conversation ends, which is the point:
        # the user can start a long task, hang up, and it finishes in the background.
        try:
            self.p.terminate()
        except Exception:
            pass


def _extract_json(text: str):
    """Best-effort: pull the first JSON object out of a model's output (it may wrap it
    in prose or ```json fences). Returns the parsed dict, or None if there's no valid
    object — callers degrade to treating the raw text as a plain summary."""
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except Exception:
        return None


def _load_memory_tail(n: int = 30) -> str:
    try:
        with open(MEMORY, encoding="utf-8") as f:
            return "".join(f.readlines()[-n:]).strip()
    except FileNotFoundError:
        return ""
    except Exception as e:
        _log(f"memory read failed: {e!r}")
        return ""


def _put_text(text: str, paste: bool = True) -> str:
    """Copy text to the clipboard and (by default) paste it into the frontmost window.
    Copy always works; auto-paste needs Accessibility — if it can't, the text is still
    on the clipboard and we tell the user to press Cmd-V."""
    text = text or ""
    if not text.strip():
        return "nothing to put"
    try:
        subprocess.run(["pbcopy"], input=text, text=True, check=True)
    except Exception as e:
        return f"couldn't reach the clipboard: {e}"
    if not paste:
        return "copied to the clipboard"
    r = subprocess.run(
        ["osascript", "-e",
         'tell application "System Events" to keystroke "v" using command down'],
        capture_output=True, text=True)
    if r.returncode != 0:
        return "copied to the clipboard — press Cmd-V to paste it in (auto-paste needs Accessibility permission)"
    return "pasted it into the front window"


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
    custom = ((cfg.get("live") or {}).get("custom_prompt") or "").strip()
    if custom:
        blocks.append("Your custom instructions (set by the user — follow these):\n" + custom)
    mem = _load_memory_tail()
    if mem:
        blocks.append(f"What you remember from before:\n{mem}")
    try:
        import memory
        k = int(((cfg.get("live") or {}).get("memory") or {}).get("recall_count", 5) or 5)
        recent = memory.recent(k)
        if recent:
            # recent() self-labels: leads with "What I've learned about you", then
            # recent conversation summaries. Use it as-is; just add the recall hint.
            blocks.append(recent + "\n\n(Use the above to stay continuous; search deeper "
                          "with the recall tool.)")
    except Exception as e:
        _log(f"recent-conversation recall failed: {e!r}")
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

    def __init__(self, on_state=None, announce=None):
        self.on_state = on_state or (lambda s: None)
        self._announce = announce          # if set, speak this aloud right after opening
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
                               text=True, timeout=120)
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
            asyncio.ensure_future(self._pump_mic())
            self.on_state("listening")
            _log("realtime session open — listening")
            if self._announce:
                await self._speak_announcement()
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
        txt = (self._announce or "")[:2500]
        self._announce = None
        config.activity("🔔  task finished — speaking result")
        await self._ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {"type": "message", "role": "user",
                     "content": [{"type": "input_text",
                                  "text": ("[A background task you started earlier just finished. "
                                           "Greet me briefly and tell me out loud, in 1-3 sentences, "
                                           "what it found. Result:\n" + txt + "\n]")}]},
        }))
        await self._ws.send(json.dumps({"type": "response.create"}))

    async def _inject_context_and_respond(self) -> None:
        self.on_state("thinking")
        # Grab text context and the screenshot concurrently so the silent gap before the
        # reply stays as short as possible (each toggle may no-op and return fast).
        ctx, shot = await asyncio.gather(
            self._loop.run_in_executor(None, _grab_context),
            self._loop.run_in_executor(None, _grab_screenshot),
        )
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
                cmd, out_path = built
                self._run_in_shell(cmd)   # nohup returns instantly; job runs detached
                _open_task_log(out_path)  # surface a live log window for the pi job
                out = "Started it in the background — I've opened its live log, and I'll come back with the result when it's done."
            config.activity(f"🚀  delegated: {args.get('instruction', '')[:80]}")
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
        config.activity(f"⚙️  ran: {command}")
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
                import numpy as np
                s = np.frombuffer(data, dtype=np.int16)
                if s.size:
                    rms = float(np.sqrt(np.mean(s.astype(np.float32) ** 2)))
                    lvl = min(1.0, rms / 4000.0)
                    # fast attack, slow decay — feels like it's catching your words
                    self.level = lvl if lvl > self.level else self.level * 0.85 + lvl * 0.15
            except Exception:
                pass
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
