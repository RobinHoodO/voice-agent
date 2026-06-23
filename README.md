# Thrivbe Voice Agent (menubar MVP)

A standalone macOS **menubar app** you talk to. Lives in the top menu bar (🎙).
Hold **Right-Option** to record, release to transcribe and answer out loud.
Click **🎙 → Start talking** is still available as a fallback.

- **Brain:** DeepSeek V3 via OpenRouter
- **Ears:** OpenAI STT (`gpt-4o-mini-transcribe`)
- **Voice:** ElevenLabs (Rachel) → macOS `say` fallback. Toggle in the menu.

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
💭 thinking · 🗣 speaking. Menu has: click-to-talk fallback, voice toggle,
reset conversation, quit.

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

## Knobs (top of `agent.py`)
- `MIC` — avfoundation device (`:2` = MacBook mic; list with
  `ffmpeg -f avfoundation -list_devices true -i ""`)
- `LLM_MODEL` — `deepseek/deepseek-chat`; any OpenRouter model works
- `ELEVEN_VOICE` — ElevenLabs voice id

## Notes
- DeepSeek on OpenRouter intermittently returns blank completions — handled with
  a retry in `think()`.
- `run_shell` runs arbitrary commands in the workspace (that's the terminal-access
  feature). Single-user personal tool — add a confirmation gate if it ever needs
  to do destructive things.

## Deliberately skipped (add when the core feels right)
- Wake word — Right-Option push-to-talk is more reliable.
- Streaming TTS — answers are short. Add if the ~1s wait bugs you.
