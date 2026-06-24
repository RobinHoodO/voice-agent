"""Back-compat shim. realtime.py was split (Phase 4) into:
  realtime_client.py  — Realtime API protocol, constants, auth headers, text self-test
  audio.py            — AudioMixin: mic capture + speaker playback
  live_session.py     — LiveSession orchestration + the __main__ self-tests

Kept so `import realtime; realtime.LiveSession(...)` (agent.py) and
`python realtime.py --selftest[-shell|-memory]` keep working unchanged.
"""
from live_session import LiveSession, main  # noqa: F401

if __name__ == "__main__":
    main()
