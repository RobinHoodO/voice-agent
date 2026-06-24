"""Characterization test for the realtime.py → 3-module split (Phase 4, modules 7-9).

LiveSession can't run headlessly (needs a mic + OpenAI socket), but we can pin that
it composes correctly: instantiates, mixes in the audio methods, keeps orchestration,
and the realtime.py shim still re-exports it for agent.py.
"""
import live_session
import realtime


def test_live_session_instantiates_with_mixin_and_orchestration():
    s = live_session.LiveSession()
    # audio methods come from AudioMixin
    for m in ("_start_audio", "_mic_cb", "_player", "_flush_out", "_teardown_audio", "_pump_mic"):
        assert callable(getattr(s, m)), f"missing audio method {m}"
    # orchestration stays on LiveSession
    for m in ("_session", "_configure", "_handle", "_do_tool", "_run_in_shell", "start", "stop"):
        assert callable(getattr(s, m)), f"missing orchestration method {m}"
    assert s.level == 0.0 and s._running is False and s._ws is None


def test_realtime_shim_reexports_livesession():
    # agent.py does `import realtime; realtime.LiveSession(...)` — must keep working.
    assert realtime.LiveSession is live_session.LiveSession
