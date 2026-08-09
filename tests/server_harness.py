"""Harness for the server-surface tests: a real uvicorn, a real LiveSession, a fake model.

The point of these tests is the transport, so everything below the backend is real —
`server.app`, `server.audio_ws`, `core.audio_core`'s mic pump and VAD, `LiveSession`'s
state machine — and only the provider websocket is faked. That is the seam
`core.backends.base.Backend` already defines, so faking it needs no production hook.
"""
from __future__ import annotations

import asyncio
import base64
import json
import math
import threading
import time

import uvicorn

from core.backends import base as events
from core.backends.base import Backend, NormalizedEvent


# ── PCM helpers ──────────────────────────────────────────────────────────────
def pcm_frame(samples: int, amplitude: int = 0, hz: float = 220.0,
              rate: int = 16000, phase: int = 0) -> bytes:
    """One mono PCM16 frame. amplitude 0 = digital silence."""
    out = bytearray()
    for i in range(samples):
        v = 0 if amplitude == 0 else int(amplitude * math.sin(2 * math.pi * hz * (phase + i) / rate))
        out += int(max(-32768, min(32767, v))).to_bytes(2, "little", signed=True)
    return bytes(out)


# ── the fake provider ────────────────────────────────────────────────────────
class FakeProviderWS:
    """Async-iterable stand-in for the provider socket. Tests push raw event dicts."""

    def __init__(self):
        self.q: asyncio.Queue = asyncio.Queue()
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        item = await self.q.get()
        if item is None:
            raise StopAsyncIteration
        return item

    async def send(self, _raw: str) -> None:
        pass

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            await self.q.put(None)


class FakeBackend(Backend):
    """Manual-VAD backend (the Gemini shape) so the tests exercise core's own turn
    boundaries rather than a server-side VAD we cannot observe."""

    mic_rate = 16000
    manual_vad = True

    def __init__(self):
        self.ws: FakeProviderWS | None = None
        self.audio_in: list[bytes] = []      # every mic chunk that reached the model
        self.activity: list[str] = []        # "start" / "end"
        self.cancels = 0
        self.responses = 0
        self.contexts: list[str] = []
        self.setup: dict | None = None
        self.tool_results: list[tuple[str, str]] = []

    async def connect(self):
        self.ws = FakeProviderWS()
        return self.ws

    async def close(self) -> None:
        if self.ws is not None:
            await self.ws.close()

    def build_setup(self, instructions, tools, voice, audio_cfg=None) -> dict:
        return {"instructions": instructions, "voice": voice, "tools": len(tools)}

    async def send_setup(self, instructions, tools, voice, audio_cfg=None) -> None:
        self.setup = self.build_setup(instructions, tools, voice, audio_cfg)

    async def send_activity_start(self) -> None:
        self.activity.append("start")

    async def send_activity_end(self) -> None:
        self.activity.append("end")

    async def send_audio_chunk(self, pcm_bytes: bytes) -> None:
        self.audio_in.append(pcm_bytes)

    async def send_text_context(self, text: str, image_b64: str | None = None) -> None:
        self.contexts.append(text)

    async def send_tool_result(self, call_id: str, output: str) -> None:
        self.tool_results.append((call_id, output))

    async def trigger_response(self) -> None:
        self.responses += 1

    async def cancel_response(self) -> None:
        self.cancels += 1

    def parse_event(self, ev: dict) -> NormalizedEvent:
        kind = ev.get("kind", events.OTHER)
        if kind == events.AUDIO_DELTA:
            return NormalizedEvent(kind=kind, audio=base64.b64decode(ev["audio_b64"]))
        return NormalizedEvent(kind=kind, text=ev.get("text", ""),
                               call_id=ev.get("call_id", ""), name=ev.get("name", ""),
                               args=ev.get("args"), detail=ev.get("detail"))

    # --- test-side injection (called from the test thread) -------------------
    def push(self, loop: asyncio.AbstractEventLoop, event: dict, timeout: float = 5.0) -> None:
        fut = asyncio.run_coroutine_threadsafe(self.ws.q.put(json.dumps(event)), loop)
        fut.result(timeout)


# ── a LiveSession that uses it ───────────────────────────────────────────────
def make_recording_factory(sessions: list):
    """A `server.app.SESSION_FACTORY` that builds real BrowserLiveSessions on a fake
    backend and records them so the test can inject provider events."""
    from server.session import BrowserLiveSession

    class RecordedSession(BrowserLiveSession):
        def _make_backend(self, cfg):
            self.fake = FakeBackend()
            return self.fake

        def _learn(self, cid, transcript):   # never shell out to `pi` in a test
            return None

    def factory(**kwargs):
        s = RecordedSession(**kwargs)
        sessions.append(s)
        return s

    return factory


class Client:
    """A scripted browser: reads the socket in the background and sorts frames into
    control JSON and model audio, the way index.html's `ws.onmessage` does."""

    def __init__(self, ws):
        self.ws = ws
        self.control: list[dict] = []
        self.audio: list[bytes] = []
        self._reader: asyncio.Task | None = None

    async def __aenter__(self) -> "Client":
        self._reader = asyncio.ensure_future(self._read())
        return self

    async def __aexit__(self, *_exc) -> None:
        if self._reader:
            self._reader.cancel()

    async def _read(self) -> None:
        try:
            async for msg in self.ws:
                if isinstance(msg, (bytes, bytearray)):
                    self.audio.append(bytes(msg))
                else:
                    self.control.append(json.loads(msg))
        except Exception:
            pass

    def of(self, kind: str) -> list[dict]:
        return [m for m in self.control if m.get("type") == kind]

    async def send_pcm(self, data: bytes) -> None:
        await self.ws.send(data)

    async def expect(self, kind: str, timeout: float = 10.0) -> dict:
        got = await await_for(lambda: (self.of(kind) or [None])[0], timeout,
                              what=f"control frame {kind!r}")
        return got


async def await_for(predicate, timeout: float = 10.0, interval: float = 0.02,
                    what: str = "condition"):
    """asyncio twin of `wait_for` — polls without blocking the client's reader task."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(interval)
    raise AssertionError(f"{what} not met within {timeout}s")


def wait_for(predicate, timeout: float = 10.0, interval: float = 0.02):
    """Poll a condition, returning its value. Raises on timeout with a readable message."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s")


# ── uvicorn on an ephemeral port ─────────────────────────────────────────────
class LiveServer:
    """Runs `server.app:app` on 127.0.0.1:<free port> in a thread, lifespan and all."""

    def __init__(self, app):
        self._config = uvicorn.Config(app, host="127.0.0.1", port=0,
                                      log_level="warning", lifespan="on")
        self._server = uvicorn.Server(self._config)
        self._thread = threading.Thread(target=self._server.run, daemon=True,
                                        name="test-uvicorn")
        self.port = 0

    def __enter__(self) -> "LiveServer":
        self._thread.start()
        deadline = time.time() + 20
        while not self._server.started:
            if time.time() > deadline or not self._thread.is_alive():
                raise RuntimeError("uvicorn did not start")
            time.sleep(0.02)
        self.port = self._server.servers[0].sockets[0].getsockname()[1]
        return self

    def __exit__(self, *_exc) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=20)

    @property
    def http(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def ws_url(self, token: str, session: str) -> str:
        return f"ws://127.0.0.1:{self.port}/live?token={token}&session={session}"
