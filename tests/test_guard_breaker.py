"""The refusal path must not fuel the loop it exists to break.

Replays the 2026-08-08 12:07 incident: the thrash guard correctly refused every
write, but each refusal called trigger_response(), the model answered every
refusal by re-issuing the same batch, and the session spun through ~40 refusals
in 12 seconds until Robin double-tapped it dead. Only the FIRST refusal in a
cooldown window may retrigger a response; the rest send the tool result silently.
"""
import asyncio
import types

from core.live_session import GUARD_RETRIGGER_COOLDOWN_S, LiveSession


class FakeBackend:
    def __init__(self):
        self.results = []
        self.triggers = 0

    async def send_tool_result(self, call_id, message):
        self.results.append((call_id, message))

    async def trigger_response(self):
        self.triggers += 1


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def time(self):
        return self.t


def _stub():
    s = types.SimpleNamespace()
    s._backend = FakeBackend()
    s._loop = FakeClock()
    s._guard_refused_at = 0.0
    s._awaiting_reply_since = None
    s._refuse_guarded_call = types.MethodType(LiveSession._refuse_guarded_call, s)
    return s


def test_refusal_storm_retriggers_exactly_once():
    """40 refusals in 12s (the incident shape) -> 40 tool results, ONE retrigger."""
    s = _stub()

    async def storm():
        for i in range(40):
            s._loop.t += 0.3
            await s._refuse_guarded_call(f"call_{i}", "THRASH GUARD: stop")

    asyncio.run(storm())
    assert len(s._backend.results) == 40      # every call_id still gets its result
    assert s._backend.triggers == 1


def test_cooldown_expiry_allows_a_fresh_spoken_refusal():
    s = _stub()

    async def two_windows():
        await s._refuse_guarded_call("a", "refused")
        s._loop.t += GUARD_RETRIGGER_COOLDOWN_S + 1
        await s._refuse_guarded_call("b", "refused")

    asyncio.run(two_windows())
    assert s._backend.triggers == 2


def test_first_refusal_arms_the_stall_watchdog():
    s = _stub()
    asyncio.run(s._refuse_guarded_call("a", "refused"))
    assert s._awaiting_reply_since == s._loop.t
