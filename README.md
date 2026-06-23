# Thrivbe Voice Agent (macOS menubar)

A standalone macOS **menubar app** you talk to. Lives in the top menu bar (🎙).
Two ways to talk to it:

| Mode | Trigger | Engine | Voice |
|------|---------|--------|-------|
| **Push-to-talk** | hold **Right-Option** | brain `openai/gpt-4o-mini` (OpenRouter) · ears `gpt-4o-mini-transcribe` | **ElevenLabs** `eleven_flash_v2_5` (Rachel) → macOS `say` fallback |
| **Live conversation** | double-tap **Control** | **OpenAI Realtime API** (`gpt-realtime`, speech-to-speech) | OpenAI voice **`alloy`** (the model speaks directly — no separate TTS) |

Click **🎙 → Start talking** is also there as a click-to-talk fallback.

Both modes read whatever you're pointing at (the UI element under your mouse
cursor + any selected text, via macOS Accessibility) and can run shell commands.

## Live conversation mode (agentic terminal)

Double-tap **Control** to start/stop a hands-free, barge-in-able conversation. A
minimal pill appears bottom-center showing state (listening / thinking /
speaking). Server-side VAD handles turn-taking; talk over it to interrupt.

In live mode the agent is a **proactive, system-wide agentic terminal**:

- **Persistent shell** — one zsh stays alive for the session; `cd`, env vars, and
  activated venvs persist between commands. Starts in your home folder and can act
  anywhere on the Mac. Self-healing: a command that runs >20s is killed and the
  shell resets (it's steered to use Spotlight `mdfind`, never `find ~`).
- **Skill / agent activation** — it can shell out to `pi -p --model deepseek-v4-flash "..."`
  (a headless AI agent with file/bash tools + your skills) to run a skill or delegate a
  bigger task (long jobs are backgrounded).
- **Cross-session memory** — a `remember` tool appends durable notes to
  `.voice-memory.log`; the recent tail is reloaded on each session start, so it
  remembers across restarts.
- **Workspace-aware** — knows about `~/Thrivbe-AI/CLAUDE.md` and the skills dirs.

> Live mode speaks in an OpenAI voice (`alloy`), **not** ElevenLabs — speech-to-speech
> is the whole point of the low latency. Push-to-talk keeps ElevenLabs.

## How it works (architecture)

The "agentic" part is live mode: instead of a request→reply turn, the agent holds
an open voice session in which it can **act on your Mac** — run shell commands,
delegate to other AI agents, and remember things — and narrate what it's doing, all
hands-free. Here's the full picture.

### Threading model

Three things run at once and must not block each other:

- **Main thread (rumps):** owns the menu bar and the floating pill. AppKit is not
  thread-safe, so the pill is only ever touched here, reconciled every 0.3s from a
  plain `self.status` string the other threads write.
- **Hotkey thread (pynput):** a global key listener. Double-tap Control toggles live
  mode; hold Right-Option is push-to-talk. Every callback is wrapped in try/except —
  an exception in a key callback would kill the whole listener (no more hotkey).
- **Live-session thread (`realtime.py`):** when live mode starts, a daemon thread
  spins up its own asyncio event loop and opens the Realtime WebSocket. All the
  socket I/O, audio, and tool calls live here. Audio in/out each get their own
  helper thread/stream on top.

Cross-thread hand-offs are deliberate: the mic (a PortAudio callback thread) pushes
bytes to the asyncio loop via `call_soon_threadsafe`; blocking shell calls run in a
thread-pool executor so they never stall the socket loop.

### A live turn, start to finish

1. **You speak.** The mic stream (24 kHz PCM16) streams to the Realtime socket as
   `input_audio_buffer.append` frames. The server's **VAD** decides when you've
   stopped (`speech_started` / `speech_stopped`).
2. **Barge-in.** If you start talking while it's speaking, on `speech_started` we
   flush the audio output queue and send `response.cancel` — it shuts up immediately.
3. **Context injection.** On `speech_stopped`, before asking for a reply, the agent
   grabs **what's under your mouse cursor right now** (the focused UI element + any
   selected text, read via the macOS Accessibility API — no clipboard) and injects it
   as a conversation item, then sends `response.create`. So every turn knows what
   you're pointing at. (VAD is configured `create_response: false` precisely so we can
   slip this context in before each response.)
4. **It thinks / talks / acts.** The model streams audio back (`output_audio.delta` →
   speaker) and/or calls a **tool**. The pill cycles listening → thinking → speaking.

### Tools — how it acts

The model is given two function tools (OpenAI Realtime "flat" tool schema):

- **`run_shell(command)`** — runs the command in the session's **persistent shell**
  (see below) and feeds stdout/stderr back as a `function_call_output`, then triggers
  another response so it can speak about the result. This is the whole "agentic
  terminal": ask it to do something, it issues real commands as you.
- **`remember(note)`** — appends a timestamped line to `.voice-memory.log`.

When a tool result comes back, the agent loops (`response.create` again), so it can
chain commands — run something, read the output, decide the next command, then
finally speak — without you saying anything in between.

### The persistent shell

`run_shell` does **not** spawn a fresh subprocess per command. Each live session
holds one long-lived `zsh` (`Shell` in `realtime.py`):

- **One process, whole session.** `cd`, exported env vars, and activated venvs persist
  between commands — it behaves like a real terminal you're dictating to. Starts in
  your home folder, so it's system-wide, not boxed to the workspace.
- **Sourced, non-interactive.** It's a non-interactive `zsh` (so no prompt characters
  pollute the output) that `source ~/.zshrc` at startup — that loads your full `PATH`
  and shell functions, including the `claude` wrapper.
- **Output capture.** After each command it prints a random sentinel marker plus the
  exit code (`print -r -- "{MARK}$?"`); the reader collects everything up to that
  marker via `select()` with a deadline, strips ANSI, and returns it. That's how it
  knows exactly where one command's output ends.
- **Self-healing.** The shell runs in its own process group (`start_new_session`). A
  command that runs longer than ~20s would otherwise wedge the shell (zsh runs piped
  input serially, so retries queue behind it) — so on timeout the whole group is
  `SIGKILL`ed and the shell respawns clean (cwd/env reset to home). The prompt steers
  the model to fast tools (Spotlight `mdfind`, known project dirs) and away from
  disk-wide scans like `find ~`.

### Activating skills and other agents

Because the persistent shell has your real `PATH` and shell functions, the agent can
run **`pi -p --model deepseek-v4-flash "<instruction>"`** — a headless `pi` agent
(read/bash/edit/write tools + your `~/.claude/skills`) running on DeepSeek V4 Flash.
That's how a quick voice request can fan out into real work ("run the front skill to
draft a reply"). Since a delegation can take a while and a blocking shell call would
freeze the conversation, the system prompt tells it to background long jobs
(`pi -p --model deepseek-v4-flash "..." > /tmp/voice-task.txt 2>&1 &`) and read the
file back when you ask how it went.

### Cross-session memory

The agent is told to call `remember(note)` whenever you state a durable fact,
preference, or task. Those lines accumulate in `.voice-memory.log` (in the project
folder, git-ignored). On **every** session start, the recent tail of that log is
folded into the session instructions under "What you remember from before" — so if you
talk to it, close it, and reopen it tomorrow, it still knows. The same startup blob
also lists the available skill categories and points it at `~/Thrivbe-AI/CLAUDE.md`.

### What stays separate

Push-to-talk is untouched by all of this — it's the narrow, safe path: hold
Right-Option → record → transcribe → one `gpt-4o-mini` turn (with its own
workspace-scoped `run_shell`) → ElevenLabs reply. The agentic loosening (persistent
shell, system-wide reach, proactive behavior, memory, skill delegation) lives only in
live mode (`realtime.py`).

## Build it

The app is packaged as a self-contained macOS bundle so Accessibility/Input
Monitoring permissions attach to **Thrivbe Voice.app**, not to a changing
Homebrew Python binary.

```
cd /Users/robinsverd/Thrivbe-AI/projects/voice-agent
./build_app.sh
```

`build_app.sh` uses `/opt/homebrew/bin/python3.12`, installs the requirements,
builds with `py2app`, copies the result to `./Thrivbe Voice.app`, and signs it
ad-hoc with bundle id `com.thrivbe.voice-agent`.

## Run it
```
cd /Users/robinsverd/Thrivbe-AI/projects/voice-agent
./run.sh
```
A 🎙 appears in your menu bar. The icon shows state: 🎙 idle · 🔴 listening ·
💭 thinking · 🗣 speaking. Menu has: click-to-talk fallback, live-conversation
toggle, voice toggle, reset conversation, quit.

### Deploying code changes without a rebuild
A rebuild changes the bundle's signature and **resets all TCC permissions**. For
pure code edits (`agent.py`, `realtime.py`, `pill.py`) copy the file into the
bundle and relaunch instead — grants are preserved:

```
cp realtime.py "Thrivbe Voice.app/Contents/Resources/realtime.py"
osascript -e 'quit app "Thrivbe Voice"'; open "Thrivbe Voice.app"
```

Only rebuild (`./build_app.sh`) when dependencies or `setup.py` change.

### First run permissions
The app runs as `Thrivbe Voice.app`, so grant permissions to **Thrivbe Voice**:

1. **Microphone** — allow when macOS prompts on first recording.
2. **Accessibility** — System Settings → Privacy & Security → Accessibility → add **Thrivbe Voice**.
3. **Input Monitoring** — System Settings → Privacy & Security → Input Monitoring → add **Thrivbe Voice**.
4. **Automation** — allow when macOS prompts for frontmost app/window/selection context.

After changing Accessibility or Input Monitoring, quit and relaunch the app:

```
osascript -e 'quit app "Thrivbe Voice"'
./run.sh
```

### Test

1. Confirm the 🎙 menu bar icon is visible.
2. Hold **Right-Option**. The icon should change to 🔴.
3. Say a short question.
4. Release **Right-Option**. The icon should change to 💭, then 🗣 while it speaks.
5. If the hotkey does nothing, re-check Accessibility and Input Monitoring for **Thrivbe Voice**.

## Always-on (auto-start at login, self-healing)
```
cp projects/voice-agent/com.thrivbe.voice-agent.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.thrivbe.voice-agent.plist
```
Logs → `projects/voice-agent/agent.log`. Unload: `launchctl unload ...plist`.
The launch agent opens `Thrivbe Voice.app`; do not launch `agent.py` directly
for normal menu-bar use.

## Knobs
Push-to-talk (top of `agent.py`):
- `MIC` — avfoundation device (auto-detected by name; list with
  `ffmpeg -f avfoundation -list_devices true -i ""`)
- `LLM_MODEL` — `openai/gpt-4o-mini`; any OpenRouter model works
- `ELEVEN_VOICE` — ElevenLabs voice id

Live mode (top of `realtime.py`):
- `MODEL` — `gpt-realtime` (GA Realtime model)
- `VOICE` — OpenAI voice id (`alloy`)
- `MEMORY` — path of the cross-session memory log

## Config / secrets
Keys load at runtime from the workspace `.env` (`~/Thrivbe-AI/.env`): needs
`OPENAI_API_KEY` (STT + Realtime), `OPENROUTER_API` (push-to-talk brain), and
`ELEVENLABS_API_KEY` (push-to-talk voice). Secrets are never committed.

## Notes
- `run_shell` runs arbitrary commands as you (workspace cwd in push-to-talk,
  system-wide persistent shell in live mode). Single-user personal tool — add a
  confirmation gate before exposing it anywhere multi-user.
- Push-to-talk on OpenRouter can return a blank completion — handled with a retry
  in `think()`.

## Deliberately skipped (add when the core feels right)
- Wake word — the push-to-talk / double-tap triggers are more reliable.
