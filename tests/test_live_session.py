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


def test_mic_task_done_callback_swallows_errors():
    # Phase 2.4: a crashed mic pump must be logged, never re-raised from the callback.
    s = live_session.LiveSession()

    class _Boom:
        def exception(self):
            return RuntimeError("mic pump died")

    s._on_mic_task_done(_Boom())   # must not raise


def test_notify_never_raises_headless():
    # Phase 2.3: surfacing failures must degrade silently when rumps isn't available.
    live_session.LiveSession()._notify("connection lost")


def test_out_q_is_bounded_and_drops_oldest():
    # Phase 3.2: a slow speaker must not let the playback queue grow without bound.
    s = live_session.LiveSession()
    for i in range(400):                      # well past maxsize
        s._enqueue_audio(bytes([i % 256]))    # must never block
    assert s._out_q.qsize() <= 256
