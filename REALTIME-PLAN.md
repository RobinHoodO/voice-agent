# Voice Agent — Live Conversation Mode (build spec)

> Pick this up in a FRESH Claude session. Full context + gotchas are in memory
> (`project_voice_agent.md`). This file is the build plan for the next feature.

## Goal
Turn the current push-to-talk agent into a **live, hands-free conversation** like
Wispr Flow:
- **Double-tap Right-Option** toggles a live session on/off.
- A small **floating always-on-top pill** appears (center/corner) showing state:
  listening ▸ thinking ▸ speaking. Click to dismiss.
- Natural **turn-taking** (it knows when you stop talking) and **barge-in**
  (you can interrupt it mid-reply).

## Decided architecture: OpenAI Realtime API (speech-to-speech)
Replace the STT→LLM→TTS chain with ONE Realtime socket.
- Endpoint: `wss://api.openai.com/v1/realtime?model=gpt-realtime` (or current
  `gpt-4o-realtime-preview`). Auth: `OPENAI_API_KEY` (already in workspace `.env`).
  Use the OpenAI Python SDK realtime client (`openai.beta.realtime`) or raw
  `websockets` — check `claude-api` skill / current docs for the live model id.
- Server-side VAD for turn detection (`turn_detection: {type: "server_vad"}`).
- Stream mic audio IN (PCM16 mono 24kHz) and play audio OUT as it arrives.
  Support interruption: on user speech, cancel the in-flight response.
- Keep the existing **tools + context**:
  - `run_shell` → register as a Realtime function tool (same handler as now).
  - Cursor/selection context (`grab_context()` already returns AX-under-cursor +
    marked text) → inject as a conversation item / instructions at session start
    and refresh per user turn.

## Audio (changes from current)
Current uses ffmpeg one-shot recording — won't work for streaming. Switch to
continuous PCM streaming:
- Add `sounddevice` (PortAudio) + `numpy` to `requirements.txt`.
- Input: 24kHz mono int16 stream → send frames to the socket.
- Output: play the model's audio frames through a sounddevice output stream.
- Keep `detect_mic()` idea but sounddevice picks the device by name/index.

## Floating pill (UI)
rumps already runs the NSApplication run loop. From it, create a PyObjC
`NSPanel` (borderless, `NSWindowStyleMaskNonactivatingPanel`, level
`NSStatusWindowLevel`, `setFloatingPanel_(True)`, transparent bg, rounded view)
showing the state + optional live transcript. Keep the 🎙 menubar item too.

## Trigger
Detect **double-tap of `keyboard.Key.alt_r`** (two press+release within ~400ms,
no other key) via the existing pynput listener → toggles live session.
Keep single hold-to-talk as a fallback if easy.

## Hard-won gotchas (DON'T relearn these — see project_voice_agent.md)
- **Permissions:** hotkey needs **Input Monitoring** (NOT Accessibility —
  `AXIsProcessTrusted()` returns False yet the key listener still fires).
  Cursor/selection AX reading needs **Accessibility**. Mic needs **Microphone**.
- **Python 3.12** (not 3.14 — breaks rumps menubar icon).
- **py2app self-contained bundle** is required so TCC grants bind to the app's
  own signed binary. Build: `./build_app.sh`.
- **Deploy code edits WITHOUT re-signing:** `cp agent.py "Thrivbe Voice.app/Contents/Resources/agent.py"`
  then relaunch. Re-signing changes the cdhash → resets all TCC grants.
- **`.env` must be read with `encoding="utf-8"`** (py2app defaults to ASCII → crash).
- Diagnostics log to `/tmp/voice-agent.log` (LOG() helper). Use it to verify.
- Model note: `google/gemini-2.0-flash-001` 404s on Robin's OpenRouter; current
  text brain is `openai/gpt-4o-mini`. Realtime uses OpenAI directly.

## Repo
`RobinHoodO/voice-agent` (private). Push changes there.

## Cost
Realtime API bills per minute of audio in/out — Robin approved this tradeoff.
```
```
