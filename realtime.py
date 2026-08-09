"""Back-compat shim. realtime.py was split (Phase 4) into what is now:
  core/realtime_client.py  — Realtime API protocol, constants, auth headers, self-test
  core/audio_core.py       — the transport-independent mic pump + VAD turn boundaries
  mac/audio.py             — the macOS PortAudio transport
  core/live_session.py     — LiveSession orchestration + the __main__ self-tests

Kept so `import realtime; realtime.LiveSession(...)` and
`python realtime.py --selftest[-shell|-memory]` keep working unchanged.
"""
from core.live_session import LiveSession, main  # noqa: F401

if __name__ == "__main__":
    main()
