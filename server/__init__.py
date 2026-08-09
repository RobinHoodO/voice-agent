"""The headless surface: reserved for the server transport (worker 2).

What lands here is the Linux mirror of `mac/`: a browser/SIP audio transport
(`core.caps.AudioTransport` — the VAD half is already in `core.audio_core` and needs
no port), a notifier, and, if a screen is ever readable remotely, a
`core.caps.ScreenContext`. Secrets need nothing: `core.secrets` already falls through
to env vars and `$CREDENTIALS_DIRECTORY`, which is what a systemd unit provides.
"""
