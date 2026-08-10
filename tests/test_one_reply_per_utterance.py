"""One utterance gets ONE reply.

Robin said "hi" and she greeted him twice, in two different ways, on both the desk and the
phone. Cause, measured against the live Gemini API on 2026-08-10: `activityEnd` is itself
the end-of-turn signal — sending it alone returned a complete reply ("Hello! How can I help
you today?") with `turnComplete` never sent. The app then called `trigger_response()` on
top, which is a second, independent generation of the same turn.

So the rule under test is not "trigger once" but "trigger only when nobody else did".
"""
import asyncio

import pytest

from core import live_session


class _Backend:
    """Records triggers. `ends_turn_on_activity_end` is the switch under test."""

    manual_vad = True

    def __init__(self, ends_turn_on_activity_end):
        self.ends_turn_on_activity_end = ends_turn_on_activity_end
        self.triggers = 0
        self.contexts = []

    async def trigger_response(self):
        self.triggers += 1

    async def send_text_context(self, text, image_b64=None):
        self.contexts.append(text)


class _Session(live_session.LiveSession):
    def __init__(self, backend):
        self._backend = backend
        self._loop = asyncio.get_event_loop()
        self._awaiting_reply_since = None
        self._last_speech = 0.0
        self._on_task_spoken = None
        self.states = []

    # Only the collaborators _inject_context_and_respond actually reaches.
    def on_state(self, s):
        self.states.append(s)

    async def _grab_screen(self, *, screenshot):
        return "", ""

    def _drain_offered_tasks(self):
        return []


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    loop.close()


def test_gemini_speech_turn_does_not_ask_for_a_second_reply(loop):
    """The regression. activityEnd already started the reply; asking again = two greetings."""
    backend = _Backend(ends_turn_on_activity_end=True)
    session = _Session(backend)
    _run(session._on_speech_stopped())
    assert backend.triggers == 0, (
        "the provider was already replying — triggering again is the second 'hi'")


def test_openai_speech_turn_still_asks_for_its_reply(loop):
    """OpenAI sets create_response:false on purpose, so the app MUST trigger. If this
    breaks, that surface goes mute rather than talkative — the opposite failure."""
    backend = _Backend(ends_turn_on_activity_end=False)
    session = _Session(backend)
    _run(session._on_speech_stopped())
    assert backend.triggers == 1


def test_the_stall_watchdog_is_armed_either_way(loop):
    """A reply is owed whoever asked for it — otherwise a provider that goes silent after
    activityEnd would never be noticed on the Gemini path."""
    for ends_turn in (True, False):
        backend = _Backend(ends_turn_on_activity_end=ends_turn)
        session = _Session(backend)
        _run(session._on_speech_stopped())
        assert session._awaiting_reply_since is not None, (
            f"watchdog not armed with ends_turn_on_activity_end={ends_turn}")


def test_the_real_backends_declare_what_they_actually_do():
    """The tests above use a double, so they pin the MECHANISM. This pins the WIRING —
    without it, flipping Gemini's flag back would break Robin's conversation and no test
    would notice. Values are measured facts, not preferences:
      Gemini  — activityEnd alone returned a full reply (probe, 2026-08-10).
      OpenAI  — turn_detection carries create_response:false, so nothing replies unasked.
    """
    from core.backends.gemini_backend import GeminiBackend
    from core.backends.openai_backend import OpenAIBackend

    assert GeminiBackend.ends_turn_on_activity_end is True
    assert OpenAIBackend.ends_turn_on_activity_end is False
    assert GeminiBackend.manual_vad is True, "the flag only matters on the manual-VAD path"


def test_non_speech_callers_always_trigger(loop):
    """Announce / stall-nudge / reconnect / tool-result had no activityEnd, so they still
    have to ask — the fix must not make her mute in those paths."""
    backend = _Backend(ends_turn_on_activity_end=True)
    session = _Session(backend)
    _run(session._inject_context_and_respond())      # default: already_replying=False
    assert backend.triggers == 1
