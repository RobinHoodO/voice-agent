"""The phone surface, end to end: browser PCM in, model audio out, barge-in in between.

Nothing is mocked below the provider socket. A real uvicorn runs `server.app:app`, a
scripted WebSocket client plays the part of `index.html`, and the audio it sends goes
through `BrowserAudioBridge` → `LiveSession._mic_cb` → `core.audio_core._pump_mic` —
the same mic pump and the same VAD numbers the Mac runs.

The two properties these tests exist to protect:

  * **no frame is swallowed.** `_pump_mic` never `continue`s inside the barge-in hold
    window because dropping those frames eats the first word of an interruption. A
    transport that batches, gates or coalesces would pass a "it talks" smoke test and
    fail this one.
  * **barge-in reaches the speaker.** Clearing the server queue is not enough when the
    speaker is a phone holding its own buffer, so the tab must be told — and the
    response must be cancelled — exactly as on the Mac.
"""
import asyncio
import base64
import json
import uuid

import pytest
from websockets.asyncio.client import connect

from server_harness import Client, await_for, pcm_frame

from conftest import VOICE_TEST_TOKEN as TOKEN

FRAME = 1600            # 100 ms at the fake backend's 16 kHz — mic_rate // 10


def _run(coro):
    return asyncio.run(coro)


async def _open(server, sessions, key=None):
    """Connect one tab and wait until its session has opened its (fake) backend."""
    key = key or str(uuid.uuid4())
    ws = await connect(server.ws_url(TOKEN, key), max_size=None)
    client = Client(ws)
    await client.__aenter__()
    await client.expect("hello")
    audio = await client.expect("audio")
    session = await await_for(lambda: sessions[-1] if sessions else None,
                              what="a LiveSession")
    await await_for(lambda: getattr(session, "fake", None), what="the backend")
    return key, ws, client, session, audio


# ── 1. the socket carries the ACTIVE backend's rate, and every frame ─────────
def test_mic_frames_all_reach_the_backend_at_the_active_rate(voice_server):
    server, sessions = voice_server

    async def scenario():
        _key, ws, client, session, audio = await _open(server, sessions)
        try:
            # Read off core's backend, never hardcoded: this fake is the Gemini shape.
            assert audio["mic_rate"] == session.fake.mic_rate == 16000
            assert audio["output_rate"] == 24000
            assert audio["frame_samples"] == 1600
            assert audio["manual_vad"] is True

            frames = [pcm_frame(FRAME, amplitude=0) for _ in range(12)]
            for f in frames:
                await client.send_pcm(f)
            await await_for(lambda: len(session.fake.audio_in) >= len(frames),
                            what="12 mic frames at the backend")
            # Byte-identical and in order — no transport buffer in between.
            assert b"".join(session.fake.audio_in[:len(frames)]) == b"".join(frames)
        finally:
            await client.__aexit__()
            await ws.close()

    _run(scenario())


# ── 2. model audio streams back down the same socket ────────────────────────
def test_model_audio_streams_back_to_the_browser(voice_server):
    server, sessions = voice_server

    async def scenario():
        _key, ws, client, session, _audio = await _open(server, sessions)
        try:
            spoken = bytes(range(256)) * 8
            session.fake.push(session._loop,
                              {"kind": "audio_delta",
                               "audio_b64": base64.b64encode(spoken).decode()})
            await await_for(lambda: client.audio, what="playback bytes")
            assert b"".join(client.audio) == spoken
            state = await await_for(lambda: [m for m in client.of("state")
                                             if m["state"] == "speaking"],
                                    what="the speaking state")
            assert state
        finally:
            await client.__aexit__()
            await ws.close()

    _run(scenario())


# ── 3. barge-in: talking over her stops playback AND cancels the response ───
def test_barge_in_stops_playback_and_never_drops_a_frame(voice_server):
    """Loud, sustained input while she is speaking must (a) cancel her response,
    (b) tell the tab to drop its buffer, and (c) still deliver every one of those
    frames to the model — including the ones inside the hold window, which is where
    the first word of the interruption lives."""
    server, sessions = voice_server

    async def scenario():
        _key, ws, client, session, _audio = await _open(server, sessions)
        try:
            session.fake.push(session._loop,
                              {"kind": "audio_delta",
                               "audio_b64": base64.b64encode(b"\x00\x01" * 512).decode()})
            await await_for(lambda: session._speaking, what="her speaking")

            before = len(session.fake.audio_in)
            loud = 0
            # ~0.6 s of speech, paced in real time so the 0.25 s barge-in hold
            # (live.vad.barge_in_hold_sec) actually elapses. amplitude 2000 → level
            # 0.5, over the 0.28 threshold_while_speaking bar.
            for i in range(20):
                await client.send_pcm(pcm_frame(FRAME, amplitude=2000, phase=i * FRAME))
                loud += 1
                await asyncio.sleep(0.03)

            await client.expect("interrupted")
            assert session.fake.cancels >= 1, "her in-flight response was not cancelled"
            assert "start" in session.fake.activity, "turn start never reached the backend"

            await await_for(lambda: len(session.fake.audio_in) >= before + loud,
                            what="every loud frame at the backend")
            assert len(session.fake.audio_in) - before >= loud, (
                "frames were swallowed during the barge-in hold window")

            # …and the turn still ends on silence: the local silence timer
            # (live.vad.silence_sec = 1.5 s) closes it once the level decays. Paced in
            # real time, because that timer is wall-clock (`loop.time()`), not a count
            # of frames — the same reason a fast-forwarded stream would not end a turn.
            for _ in range(70):
                await client.send_pcm(pcm_frame(FRAME, amplitude=0))
                await asyncio.sleep(0.05)
            await await_for(lambda: "end" in session.fake.activity, timeout=12,
                            what="end of turn")
        finally:
            await client.__aexit__()
            await ws.close()

    _run(scenario())


# ── 4. the browser cannot drive turn boundaries itself ──────────────────────
def test_client_activity_controls_are_ignored(voice_server):
    """Endpointing lives in core. A client-sent activity_start/end must never reach the
    backend, or two VADs would fight over the same turn."""
    server, sessions = voice_server

    async def scenario():
        _key, ws, client, session, _audio = await _open(server, sessions)
        try:
            for kind in ("activity_start", "activity_end", "response.create"):
                await ws.send(json.dumps({"type": kind}))
            await ws.send(json.dumps({"type": "ping"}))
            await client.expect("pong")          # proves the frames were processed
            assert session.fake.activity == []
            assert session.fake.responses == 0
        finally:
            await client.__aexit__()
            await ws.close()

    _run(scenario())


# ── 4b. the microphone is not gated on the prompt build ─────────────────────
def test_the_tab_may_start_its_microphone_before_the_prompt_is_built(voice_server,
                                                                     monkeypatch):
    """index.html drops every captured frame until the `audio` frame lands (it does not
    know what rate to resample to), so whatever that frame waits on is dead air at the
    top of the conversation — measured at 2.3 s over the tailnet, which is most of a
    first sentence.

    It used to wait on `_configure`: kernel persona, memory recall, the high-stakes
    manifest. Nothing in there tells the tab anything about audio. The slow build below
    stands in for that, and the assertion is ordering, not a stopwatch: the tab is
    configured while the prompt is still being assembled.
    """
    import threading
    import time

    from core import live_session

    # threading.Events, not asyncio ones: the prompt is built on the SESSION's thread,
    # and the test watches from the client's loop.
    building, release = threading.Event(), threading.Event()

    def slow_build(ctx, cfg=None, **kwargs):
        building.set()
        while not release.is_set():
            time.sleep(0.01)
        return "test instructions"

    monkeypatch.setattr(live_session, "_build_live_instructions", slow_build)
    server, sessions = voice_server

    async def scenario():
        ws = await connect(server.ws_url(TOKEN, str(uuid.uuid4())), max_size=None)
        client = Client(ws)
        await client.__aenter__()
        try:
            await client.expect("hello")
            await await_for(building.is_set, what="the prompt build to start")
            # THE assertion: the audio config arrives while the prompt is still building.
            audio = await client.expect("audio", timeout=5)
            assert audio["mic_rate"] == 16000
            assert not release.is_set(), "the prompt build finished first"
        finally:
            release.set()
            await client.__aexit__()
            await ws.close()

    _run(scenario())


# ── 5. transcripts and lifecycle reach the tab ──────────────────────────────
def test_transcripts_and_end_of_session_reach_the_tab(voice_server):
    server, sessions = voice_server

    async def scenario():
        _key, ws, client, session, _audio = await _open(server, sessions)
        try:
            session.fake.push(session._loop,
                              {"kind": "user_transcript", "text": "what's on today"})
            session.fake.push(session._loop,
                              {"kind": "agent_transcript", "text": "three things"})
            await await_for(lambda: len(client.of("turn")) >= 2, what="both turns")
            turns = client.of("turn")
            assert ("you", "what's on today") in [(t["role"], t["text"]) for t in turns]
            assert ("agent", "three things") in [(t["role"], t["text"]) for t in turns]

            session.stop()                        # what a spoken sign-off does
            await client.expect("ended")
        finally:
            await client.__aexit__()
            await ws.close()

    _run(scenario())


# ── 6. the PWA shell is actually served ─────────────────────────────────────
@pytest.mark.parametrize("path,needle", [
    ("/", "capture-processor"),
    ("/manifest.json", '"start_url"'),
    ("/sw.js", "CACHE_NAME"),
])
def test_pwa_shell_is_served(voice_server, path, needle):
    import httpx
    server, _sessions = voice_server
    r = httpx.get(server.http + path, timeout=10)
    assert r.status_code == 200, r.text
    assert needle in r.text


def test_ping_is_unauthenticated_and_leaks_nothing(voice_server):
    import httpx
    server, _sessions = voice_server
    r = httpx.get(server.http + "/ping", timeout=10)
    assert r.status_code == 200
    assert r.text.strip() == "ok"


# ── 7. the rate is the real backend's, not a constant ───────────────────────
@pytest.mark.parametrize("module,cls,mic_rate,manual", [
    ("core.backends.openai_backend", "OpenAIBackend", 24000, False),
    ("core.backends.gemini_backend", "GeminiBackend", 16000, True),
])
def test_transport_sends_the_real_backends_rate(module, cls, mic_rate, manual):
    """Whatever `core`'s active backend asks for is what the browser is told. Wired to
    the real backend classes so a future third backend, or a changed rate, cannot
    silently disagree with what the capture worklet is configured to produce."""
    import importlib
    import queue as _queue
    import threading

    from server.audio_ws import BrowserAudioTransport

    backend = getattr(importlib.import_module(module), cls)()

    class _Bridge:
        session_key = "0" * 8
        mic_rate = 0
        audio_config = None      # the format this tab was last told, per BrowserAudioBridge

        def __init__(self):
            self.sent = []

        def send_json(self, obj):
            self.sent.append(obj)

    class _Session:
        _backend = backend
        _audio_stop = threading.Event()
        _out_q = _queue.Queue()
        _running = False        # the player exits on the first check
        _player_thread = None

    s = _Session()
    s._bridge = _Bridge()
    BrowserAudioTransport().start(s)
    s._player_thread.join(timeout=5)

    frame = s._bridge.sent[0]
    assert frame["type"] == "audio"
    assert frame["mic_rate"] == backend.mic_rate == mic_rate
    assert frame["frame_samples"] == mic_rate // 10      # 100 ms, as mac.audio uses
    assert frame["manual_vad"] is manual
    assert frame["output_rate"] == 24000                 # core.realtime_client.SR
