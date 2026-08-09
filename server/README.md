# server/ — the phone surface

The same brain as the Mac, reached from an iPhone. `core/` is untouched: the browser
supplies a microphone and a speaker, and `core.audio_core` decides every turn boundary
exactly as it does behind PortAudio.

```
iPhone PWA ──wss /live?token=…&session=<uuid>──▶ FastAPI (server/app.py)
   AudioWorklet mic  ──PCM16 binary frames──▶  bridge.feed_mic → LiveSession._mic_cb
                                                    → _mic_q → core.audio_core._pump_mic
                                                    → VAD / turn boundaries → backend
   AudioWorklet out ◀──PCM16 binary frames──   player thread ← LiveSession._out_q
   {"type":"interrupted"} ◀──────────────────   BrowserLiveSession._flush_out (barge-in)
```

| File | Role |
|---|---|
| `app.py` | FastAPI: auth boundary, session registry, the `/live` WebSocket. |
| `audio_ws.py` | `BrowserAudioBridge` + `BrowserAudioTransport` — the Linux mirror of `mac/audio.py`. |
| `session.py` | `BrowserLiveSession`: four surface hooks over `core.live_session.LiveSession`. |
| `index.html` | The installable PWA. Two AudioWorklets, no VAD. |
| `manifest.json` / `sw.js` / `icon-*.png` | PWA shell. Distinct id + cache name from voice-bridge's. |
| `voice-agent.service` | systemd unit. **Written, not installed.** |

## The three things that are easy to get wrong

1. **Never gate the mic.** Every captured frame goes on the socket the instant it
   exists — while Pam is talking, and inside the barge-in hold window. `_pump_mic`
   never `continue`s in that window on purpose; a client-side "don't send while she
   speaks" optimisation eats the first word of every interruption.
2. **Sample rate comes from the backend, not from this code.** `BrowserAudioTransport.
   start` reads `s._backend.mic_rate` (Gemini 16 kHz, OpenAI 24 kHz) and sends it as the
   `audio` control frame; the capture worklet configures itself from that frame and
   captures nothing before it arrives. Playback is 24 kHz — `core.realtime_client.SR`.
   Both worklets linear-resample against `AudioContext.sampleRate`, which the phone
   picks (usually 48 kHz) and which nothing here may assume.
3. **Session state is per tab.** One `LiveSession` object per session UUID, so the one
   staged high-stakes action lives on that object. A spoken "yes" in another tab reaches
   a different object and cannot confirm it. `tests/test_server_auth.py::
   test_two_tabs_never_cross_confirm` is the regression guard.

## Auth

voice-bridge's pattern, kept because it has survived a year on Robin's phone:

* shared token — `X-Voice-Token` on HTTP, `?token=` on the WebSocket (browsers cannot
  set custom headers on a WS handshake), compared with `hmac.compare_digest`;
* per-tab session UUID, rejected unless it parses as a UUID;
* `_RedactTokenFilter` on `uvicorn.access` **and** `uvicorn.error` — the WebSocket
  "accepted" line comes from the error logger, and the token is in the path.

Resolution order for the token is `core.secrets`: store → `VOICE_AGENT_TOKEN` →
`$CREDENTIALS_DIRECTORY/voice_agent_token`. The unit uses the third.

## Local run (Mac, no TLS)

```bash
VOICE_AGENT_TOKEN=dev-token \
  .venv/bin/python -m uvicorn server.app:app --host 127.0.0.1 --port 8767
```

`http://127.0.0.1:8767/` — localhost counts as a secure context, so `getUserMedia` and
AudioWorklet work without a certificate. Any other host needs HTTPS.

## Deploying to Thrivbe-1 — NOT DONE YET

voice-bridge is still the live phone assistant on `:8765`. Nothing here has been
installed on the server. When it is time:

```bash
ssh hetzner 'mkdir -p /opt/voice-agent/secrets /opt/voice-agent/server /opt/voice-agent/core'
# code
rsync -a core/ hetzner:/opt/voice-agent/core/
rsync -a server/ hetzner:/opt/voice-agent/server/
# venv + deps
ssh hetzner 'python3 -m venv /opt/voice-agent && /opt/voice-agent/bin/pip install -r /opt/voice-agent/server/requirements.txt'
# secrets (server-only files, never in the repo)
ssh hetzner 'install -m0600 /dev/stdin /opt/voice-agent/secrets/voice_agent_token <<< "<token>"'
ssh hetzner 'install -m0600 /dev/stdin /opt/voice-agent/secrets/openai <<< "<key>"'
# unit
scp server/voice-agent.service hetzner:/etc/systemd/system/
ssh hetzner 'systemctl daemon-reload && systemctl enable --now voice-agent && curl -sk https://localhost:8767/ping'
```

Port choice: **8767**. Thrivbe-1's listeners on 2026-08-09 were
`22 53 443 631 3000 3002 3005 3006 3011 3015 3016 3017 3019 3020 3021 3025 5434 6380
8000 8080 8443 8444 8446 8450 8451 8642 8765 8766 8787 8790 8792 8793 9000 9119 20128`
— 8767 is free and 8765/8766 stay voice-bridge's. If it should also present as its own
Tailscale-managed HTTPS app (the trick voice-bridge uses on `:8451` so Android does not
confuse the two installs), run a second plain-HTTP instance on 127.0.0.1:8768 behind
`tailscale serve --https=8452`; 8452 was free on the same date.

TLS reuses `/opt/voice-bridge/certs/{cert.crt,cert.key}` in place — server-only files,
one certificate to renew, and `cert.key` is root-only, which is why the unit runs as
root exactly like `voice-bridge.service`.

## Known gaps on this surface

Both are the runtime `core → Mac` leaks named in `ARCHITECTURE.md §7`, and both are
**decided here rather than discovered here**:

* `core/shell.py` hard-codes `/bin/zsh`, so `Shell()` raises on Linux. `_session()`
  already catches that and logs it, so the conversation opens normally and `run_shell`
  answers `error: shell not started`. It is off the schema anyway unless
  `live.agentic_shell` is on, which it is not by default. **Decision: leave it.** The
  capture-loop race documented in that section means a shell spec behind `core.caps`
  has to come with a rewritten capture loop, and half of that fix ships something that
  looks portable and is not.
* `core/tools.py` HERDR path — `_herdr()` already swallows everything and returns
  `None`, so lane tools degrade to "no lanes" here. A server-side delegation backend
  belongs behind that same seam; `os_delegate` (kernel-side) works today and covers it.
