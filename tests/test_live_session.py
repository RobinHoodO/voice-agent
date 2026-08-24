"""Characterization test for the realtime.py → 3-module split (Phase 4, modules 7-9).

LiveSession can't run headlessly (needs a mic + OpenAI socket), but we can pin that
it composes correctly: instantiates, mixes in the audio methods, keeps orchestration,
and the realtime.py shim still re-exports it for agent.py.
"""
import asyncio
import json

from core import kernel_tools
from core import live_session
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


def test_kernel_decision_confirmation_gate_is_short_and_deny_wins():
    pending = {"args": {"approvalId": 7, "decision": "approve"}, "ts": 100.0}
    assert live_session._is_short_affirm("go ahead")
    assert not live_session._is_short_affirm("yes please record the decision now")
    assert live_session._pending_confirmation_outcome(pending, "yes", now=101.0) == "confirmed"
    assert live_session._pending_confirmation_outcome(pending, "yes no", now=101.0) == "denied"
    assert live_session._pending_confirmation_outcome(pending, "not now", now=101.0) == "dropped"
    assert live_session._pending_confirmation_outcome(pending, "yes", now=220.0) == "expired"


def test_kernel_decision_is_staged_then_executes_only_after_affirm(monkeypatch):
    calls, sent = [], []

    class Ws:
        async def send(self, message):
            sent.append(json.loads(message))

    monkeypatch.setattr(live_session.config, "activity", lambda _message: None)
    monkeypatch.setattr(kernel_tools, "kernel_decide", lambda args: calls.append(args) or "Decision recorded.")

    async def scenario():
        session = live_session.LiveSession()
        session._loop = asyncio.get_running_loop()
        session._ws = Ws()
        await session._do_tool({
            "call_id": "call-1", "name": "kernel_decide",
            "arguments": json.dumps({"approvalId": 9, "decision": "approve"}),
        })
        assert calls == []
        assert session._pending_action["tool"] == "kernel_decide"
        assert session._pending_action["args"]["approvalId"] == 9
        assert "CONFIRMATION REQUIRED" in sent[0]["item"]["output"]

        await session._resolve_pending_action("yes")

    asyncio.run(scenario())
    assert calls == [{"approvalId": 9, "decision": "approve"}]
    assert "Decision recorded." in sent[-1]["item"]["content"][0]["text"]


def test_rejected_resume_handle_is_dropped_so_reconnect_can_recover(monkeypatch):
    """A resumed session refused by the provider must not be retried with the same
    handle. 2026-08-23: one drop mid-answer, then five 1007 'Precondition check failed'
    closes ~2s apart — Robin had been heard and transcribed, and never got an answer.

    Two attempts here: the first fails fast while resuming (handle must be dropped), the
    second fails fast with no handle (nothing to drop) and ends the loop.
    """
    s = live_session.LiveSession()
    s._resume_handle = "stale-handle"
    s._running = True
    attempts = []

    async def _instant_failure():
        attempts.append(s._resume_handle)
        if len(attempts) >= 2:
            s._running = False          # stop the loop after the second attempt
        raise ConnectionError("Close(1007, 'Precondition check failed.')")

    monkeypatch.setattr(s, "_session", _instant_failure)
    monkeypatch.setattr(s, "_teardown_audio", lambda: None)
    monkeypatch.setattr(s, "_persist_conversation", lambda: None)
    monkeypatch.setattr(s, "_notify", lambda msg: None)
    monkeypatch.setattr(s, "on_state", lambda state: None)
    monkeypatch.setattr(live_session.time, "sleep", lambda n: None)

    s._run()

    assert attempts == ["stale-handle", None], (
        "the second attempt must reconnect cold, not re-send the refused handle")
    assert s._resume_handle is None


def test_slow_session_keeps_its_resume_handle(monkeypatch):
    """The guard must only fire on a fast refusal. A session that ran a real
    conversation and then dropped is a network blip — resuming it is the whole point
    of keeping the handle, so it must survive."""
    s = live_session.LiveSession()
    s._resume_handle = "good-handle"
    s._running = True
    clock = iter([0.0, live_session.RESUME_POISON_S + 10.0])

    async def _long_then_drop():
        s._running = False
        raise ConnectionError("network blip")

    monkeypatch.setattr(s, "_session", _long_then_drop)
    monkeypatch.setattr(s, "_teardown_audio", lambda: None)
    monkeypatch.setattr(s, "_persist_conversation", lambda: None)
    monkeypatch.setattr(s, "on_state", lambda state: None)
    monkeypatch.setattr(live_session.time, "monotonic", lambda: next(clock))

    s._run()

    assert s._resume_handle == "good-handle"
