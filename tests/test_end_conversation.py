"""Spoken sign-off ends the session: "thank you, that was all" → goodbye → close.

The close must wait for the goodbye audio to finish, and must ABORT if Robin keeps
talking — "thanks, that was all… oh wait" may never hang up on him.
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


def test_speaking_again_aborts_the_close():
    host = _Host()

    async def run():
        host._loop = asyncio.get_running_loop()
        # A new spoken turn lands right after the tool call:
        host._last_speech = host._loop.time() + 100
        await host._end_after_goodbye()

    asyncio.run(run())
    assert not host.stopped and not host.auto_stopped


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
