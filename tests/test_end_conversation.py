"""Spoken sign-off ends the session: "thank you, that was all" → goodbye → close.

The close waits for the goodbye audio to finish, then hangs up UNCONDITIONALLY —
Robin asked for a hard hang-up. There is deliberately no abort-on-speech: the local
VAD re-fires on mic blips, which made an abort trigger ~1s after every staging on
2026-08-08 (the close never happened). Changed his mind = double-tap for a fresh one.
"""
import asyncio
import queue
import types

import tools
from live_session import LiveSession

BY_NAME = {t["name"]: t for t in tools.TOOLS}


def test_tool_registered_with_no_required_args():
    t = BY_NAME["end_conversation"]
    assert t["parameters"]["required"] == []
    d = t["description"].lower()
    assert "that was all" in d, "must name the trigger phrase Robin actually uses"
    assert "not one" in d, "a mid-task 'thanks' must be called out as NOT a sign-off"


def test_unanswered_wake_closes_at_45s():
    """She woke Robin and he never replied — close at 45s, not the 90s idle default."""
    from live_session import _idle_reason
    args = dict(last_speech=0.0, session_start=0.0, idle_s=90, max_s=300,
                unanswered_s=45, was_announce=True, user_replied=False)
    assert _idle_reason(now=40.0, **args) is None
    reason = _idle_reason(now=50.0, **args)
    assert reason and "no reply" in reason


def test_a_real_reply_restores_the_normal_thresholds():
    from live_session import _idle_reason
    args = dict(last_speech=48.0, session_start=0.0, idle_s=90, max_s=300,
                unanswered_s=45, was_announce=True, user_replied=True)
    assert _idle_reason(now=50.0, **args) is None          # replied — 45s guard off
    assert "idle" in _idle_reason(now=140.0, **args)       # normal 90s idle still works


def test_user_initiated_sessions_are_untouched():
    """Double-tap sessions never had an announce — the 45s guard must not apply."""
    from live_session import _idle_reason
    assert _idle_reason(now=60.0, last_speech=55.0, session_start=0.0,
                        idle_s=90, max_s=300, unanswered_s=45,
                        was_announce=False, user_replied=False) is None


class _Host:
    """Just the attributes _end_after_goodbye touches — no AppKit, no websocket."""

    def __init__(self):
        self._running = True
        self._last_speech = 0.0
        self._awaiting_reply_since = None
        self._out_q = queue.Queue()
        self.stopped = False
        self.auto_stopped = False
        self._on_auto_stop = lambda: setattr(self, "auto_stopped", True)
        self._end_after_goodbye = types.MethodType(LiveSession._end_after_goodbye, self)

    def stop(self):
        self.stopped = True
        self._running = False


def test_closes_after_the_goodbye_drains():
    host = _Host()

    async def run():
        host._loop = asyncio.get_running_loop()
        await host._end_after_goodbye()

    asyncio.run(run())
    assert host.stopped and host.auto_stopped


def test_mic_blips_do_not_abort_the_close():
    """The 2026-08-08 regression: VAD speech-start fired ~1s after staging and the
    session never closed. A sign-off must hang up even if the mic hears something."""
    host = _Host()

    async def run():
        host._loop = asyncio.get_running_loop()
        host._last_speech = host._loop.time() + 100   # VAD blip right after the tool call
        await host._end_after_goodbye()

    asyncio.run(run())
    assert host.stopped and host.auto_stopped


def test_undrained_audio_defers_until_played_out():
    """While goodbye chunks are still queued the session must stay open; once the
    queue drains it closes. (The 15s cap keeps a wedged player from holding the mic.)"""
    host = _Host()
    host._out_q.put(b"chunk")

    async def run():
        host._loop = asyncio.get_running_loop()

        async def drain():
            await asyncio.sleep(0.8)
            host._out_q.get_nowait()

        await asyncio.gather(host._end_after_goodbye(), drain())

    asyncio.run(run())
    assert host.stopped
