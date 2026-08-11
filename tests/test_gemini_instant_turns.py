"""Regression coverage for the Gemini screen-context latency and tool-loop incidents.

These tests pin provider wire order and the real LiveSession event lifecycle.  The old
helper-only tests missed both bugs because they supplied no screen context and never
passed Gemini's interruption event back through the session.
"""
import asyncio
import json

from core import live_session
from core.backends import base as events
from core.backends.base import NormalizedEvent
from core.backends.gemini_backend import GeminiBackend


class _Wire:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


def test_gemini_screen_context_is_realtime_input_inside_one_activity_window():
    """Gemini 3.1 must see text/video before the single activityEnd boundary."""
    async def scenario():
        backend = GeminiBackend()
        backend.ws = _Wire()
        await backend.send_activity_start()
        await backend.send_text_context("screen text", image_b64="jpeg-base64")
        await backend.send_activity_end()
        return backend.ws.sent

    sent = asyncio.run(scenario())
    assert [next(iter(message)) for message in sent] == [
        "realtimeInput", "realtimeInput", "realtimeInput", "realtimeInput"]
    assert sent[0] == {"realtimeInput": {"activityStart": {}}}
    assert sent[1] == {"realtimeInput": {"video": {
        "mimeType": "image/jpeg", "data": "jpeg-base64"}}}
    assert sent[2] == {"realtimeInput": {"text": "screen text"}}
    assert sent[3] == {"realtimeInput": {"activityEnd": {}}}
    assert not any("clientContent" in message for message in sent)


def test_gemini_synthetic_context_opens_and_closes_its_own_activity():
    """Announcements, reconnects and watchdog nudges still produce one spoken turn."""
    async def scenario():
        backend = GeminiBackend()
        backend.ws = _Wire()
        await backend.send_text_context("announce this", image_b64="jpeg-base64")
        await backend.trigger_response()
        return backend.ws.sent

    sent = asyncio.run(scenario())
    assert sent[0] == {"realtimeInput": {"activityStart": {}}}
    assert sent[-1] == {"realtimeInput": {"activityEnd": {}}}
    assert sum("activityStart" in m.get("realtimeInput", {}) for m in sent) == 1
    assert sum("activityEnd" in m.get("realtimeInput", {}) for m in sent) == 1
    assert not any("clientContent" in message for message in sent)


def test_screen_capture_is_prefetched_and_sent_before_gemini_activity_end(monkeypatch):
    """The user's speech and silence window hide capture time from response latency."""
    async def scenario():
        session = live_session.LiveSession()
        backend = GeminiBackend()
        backend.ws = _Wire()
        session._backend = backend
        session._loop = asyncio.get_running_loop()
        captured = asyncio.Event()

        async def grab(*, screenshot):
            assert screenshot is True
            captured.set()
            return "App: Browser\nTitle: Current page", "jpeg-base64"

        monkeypatch.setattr(session, "_grab_screen", grab)
        await backend.send_activity_start()
        await session._on_speech_started()
        await asyncio.wait_for(captured.wait(), timeout=0.2)
        await session._before_activity_end()
        await backend.send_activity_end()
        await session._on_speech_stopped()
        return backend.ws.sent

    sent = asyncio.run(scenario())
    kinds = [next(iter(message)) for message in sent]
    assert kinds == ["realtimeInput"] * 4
    assert sent[-1] == {"realtimeInput": {"activityEnd": {}}}
    assert sum("activityEnd" in message.get("realtimeInput", {}) for message in sent) == 1


def test_slow_screen_prefetch_never_adds_seconds_after_speech(monkeypatch):
    async def scenario():
        session = live_session.LiveSession()
        session._loop = asyncio.get_running_loop()
        never = asyncio.Event()

        async def grab(*, screenshot):
            await never.wait()
            return "too late", "jpeg-base64"

        monkeypatch.setattr(session, "_grab_screen", grab)
        session._screen_context_task = asyncio.create_task(session._capture_screen_context())
        started = session._loop.time()
        result = await session._get_turn_screen_context()
        return result, session._loop.time() - started

    result, elapsed = asyncio.run(scenario())
    assert result == ("", "")
    assert elapsed < 0.6, f"slow screen capture added {elapsed:.2f}s after speech"


def test_gemini_interruption_is_not_fabricated_user_speech():
    """A client/protocol interruption must not clear per-user-turn loop history."""
    event = GeminiBackend().parse_event({"serverContent": {"interrupted": True}})
    assert event.kind == "response_interrupted"


def test_response_interruption_preserves_current_user_turn_guards():
    async def scenario():
        session = live_session.LiveSession()
        session._loop = asyncio.get_running_loop()
        session._recent_tools = [(1.0, "notion_list_tasks:{}")]
        session._turn_tool_calls = 7
        await session._handle_normalized(NormalizedEvent(events.RESPONSE_INTERRUPTED))
        return session._recent_tools, session._turn_tool_calls

    recent, calls = asyncio.run(scenario())
    assert recent == [(1.0, "notion_list_tasks:{}")]
    assert calls == 7


class _ToolBackend:
    def __init__(self, *, response_continues):
        self.tool_response_starts_continuation = response_continues
        self.results = []
        self.triggers = 0

    async def send_tool_result(self, call_id, output):
        self.results.append((call_id, output))

    async def trigger_response(self):
        self.triggers += 1


def _tool_session(backend):
    session = live_session.LiveSession()
    session.PROFILE = "mac"
    session._backend = backend
    session._loop = asyncio.get_running_loop()
    session._journal = lambda *_args, **_kwargs: None
    return session


def test_gemini_tool_response_continues_without_interrupting_client_content(monkeypatch):
    """Google's Live flow continues from toolResponse; an extra trigger interrupts it."""
    monkeypatch.setattr(live_session, "_put_text", lambda _text, _paste=True: "copied")

    async def scenario():
        backend = _ToolBackend(response_continues=True)
        session = _tool_session(backend)
        await session._do_tool({"call_id": "c1", "name": "put_text",
                                "arguments": json.dumps({"text": "hello"})})
        return backend, session

    backend, session = asyncio.run(scenario())
    assert len(backend.results) == 1
    assert backend.triggers == 0
    assert session._awaiting_reply_since is not None


def test_openai_tool_result_keeps_its_explicit_response_trigger(monkeypatch):
    """Provider-aware continuation must not make OpenAI tool turns go silent."""
    monkeypatch.setattr(live_session, "_put_text", lambda _text, _paste=True: "copied")

    async def scenario():
        backend = _ToolBackend(response_continues=False)
        session = _tool_session(backend)
        await session._do_tool({"call_id": "c1", "name": "put_text",
                                "arguments": json.dumps({"text": "hello"})})
        return backend

    backend = asyncio.run(scenario())
    assert len(backend.results) == 1
    assert backend.triggers == 1


def test_hard_turn_budget_refuses_work_even_when_tool_names_vary(monkeypatch):
    """A model cannot evade the work ceiling by rotating through different tools."""
    ran = []
    monkeypatch.setattr(live_session.services, "notion_search",
                        lambda args: ran.append(args) or "should not run")

    async def scenario():
        backend = _ToolBackend(response_continues=True)
        session = _tool_session(backend)
        session._turn_tool_calls = live_session.TOOL_TURN_LIMIT
        await session._do_tool({"call_id": "c-limit", "name": "notion_search",
                                "arguments": json.dumps({"query": "invoices"})})
        return backend

    backend = asyncio.run(scenario())
    assert ran == []
    assert len(backend.results) == 1
    assert "TURN TOOL LIMIT" in backend.results[0][1]


def test_only_real_user_speech_resets_the_hard_turn_budget():
    async def scenario():
        session = live_session.LiveSession()
        session._loop = asyncio.get_running_loop()
        session._turn_tool_calls = 7
        await session._on_speech_started()
        return session._turn_tool_calls

    assert asyncio.run(scenario()) == 0
