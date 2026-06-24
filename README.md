# Thrivbe Voice (macOS menubar)

A standalone macOS **menubar app** you talk to — one hands-free, agentic voice
conversation. **Double-tap Control** to start/stop it; a small glowing glass **orb**
appears at the bottom-center of your screen while it's live. It speaks back in an
OpenAI voice (speech-to-speech, low latency — no separate TTS), reads what's under
your cursor, can run an agentic shell, and remembers across sessions.

- **Engine:** OpenAI **Realtime API** (`gpt-realtime`), speech-to-speech.
- **The only key you need:** an OpenAI API key (stored in your macOS Keychain).

## Live conversation (the whole app)

Double-tap **Control** to start/stop a hands-free, barge-in-able conversation.
Server-side VAD handles turn-taking; talk over it to interrupt. A frosted-glass
**orb** shows state by colour + a slow breathing glow — mint = listening,
periwinkle = thinking, amber = speaking (bottom-center, no text).

The agent is a **proactive, system-wide agentic terminal**:

- **Persistent shell** — one zsh stays alive for the session; `cd`, env vars, and
  activated venvs persist. Starts in your home folder, acts anywhere on the Mac.
  Self-healing: a command over ~20s is killed and the shell resets (steered to
  Spotlight `mdfind`, never `find ~`). **Off by default** — enable via the
  **Agentic shell** menu toggle.
- **Skill / agent activation** — shells out to `pi -p --model deepseek-v4-flash "..."`
  (a headless AI agent with file/bash tools + your skills); long jobs backgrounded.
- **Cross-session memory** — a `remember` tool appends notes to a memory log; the
  recent tail is reloaded each session start, so it remembers across restarts.
- **Cursor-aware** — each turn it sees the UI element + selected text under your mouse.

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
- **Hotkey thread (pynput):** a global key listener — a double-tap of Control toggles
  the live session. Every callback is wrapped in try/except, since an exception in a
  key callback would kill the whole listener (no more hotkey).
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

### Audio devices

Pick your mic and speaker from the menu (**🎙 Microphone** / **🔊 Speaker**) — it lists
every device, checkmarks the current choice, and applies on the next live session.
Tip: don't use a Bluetooth headset as the *mic* — macOS drops it into low-quality
"call mode" and playback gets quiet. The smart default captures from the built-in mic
and plays to a headset if one is present.

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
💭 thinking · 🗣 speaking. Menu has: live-conversation toggle, agentic-shell toggle,
🎙 Microphone / 🔊 Speaker pickers, Set OpenAI key, Run setup again, Quit.

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
2. **Double-tap Control.** The glass orb should appear at the bottom-center (mint).
3. Say a short question, then pause → orb goes periwinkle (thinking), then amber while
   it speaks back out loud.
4. Double-tap Control again to end; the orb fades out.
5. If nothing happens, re-check Input Monitoring + Accessibility for **Thrivbe Voice**.

## Always-on (auto-start at login, self-healing)
```
cp projects/voice-agent/com.thrivbe.voice-agent.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.thrivbe.voice-agent.plist
```
Logs → `projects/voice-agent/agent.log`. Unload: `launchctl unload ...plist`.
The launch agent opens `Thrivbe Voice.app`; do not launch `agent.py` directly
for normal menu-bar use.

## Knobs (top of `realtime.py`)
- `MODEL` — `gpt-realtime` (GA Realtime model)
- `VOICE` — default OpenAI voice id (`alloy`); overridable via `config live.voice`
- `MIC_NAME` / `OUT_NAME` — default device name-substrings when none is picked
- `MEMORY` — path of the cross-session memory log

Per-session behaviour (mic/speaker, voice, delegation, shell timeout, agentic-shell)
comes from config — see below — not from constants.

## Config / secrets
Per-user settings live in `~/Library/Application Support/ThrivbeVoice/config.json`
(`config.py`); the **OpenAI API key is stored in the macOS Keychain** (service
`ThrivbeVoice`), not on disk. Enter it via the menu (**Set OpenAI key…**) or first-run
setup. OpenAI is the only key needed. Dev fallback: the key is also read from
`~/Thrivbe-AI/.env` if present, so the original workspace setup keeps working.

First launch runs **onboarding** (paste OpenAI key → deep-links the Microphone /
Accessibility / Input Monitoring panes). Re-run any time via **Run setup again…**.

> Productizing this (App Store reality, distribution, licensing, monetization) is
> planned in **[PRODUCT.md](PRODUCT.md)**; tester install steps in **[INSTALL.md](INSTALL.md)**.

## Notes
- `run_shell` runs arbitrary commands as you via the system-wide persistent shell.
  It's **off by default** (the Agentic shell toggle gates it) — a real safety boundary
  for a tool you might hand to other people.
