"""The headless surface: browser mic in, model audio out, `core` unchanged in between.

The Linux mirror of `mac/`. `server.audio_ws` is the `core.caps.AudioTransport` for a
browser tab (the VAD half needed no port — it is `core.audio_core`), `server.session`
registers the surface's capabilities and hooks the four places a tab differs from a
Mac, and `server.app` is the FastAPI/WebSocket transport plus the auth boundary.
Secrets need nothing: `core.secrets` already falls through to env vars and
`$CREDENTIALS_DIRECTORY`, which is what the systemd unit provides.

Read `server/README.md` before changing the audio path — the mic must never be gated
client-side, and the sample rate is the active backend's, never a constant.
"""
