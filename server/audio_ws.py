"""Browser audio transport — `mac/audio.py`'s sibling, in the same process.

`mac.audio.MacAudioTransport` owns this Mac's PortAudio devices; this owns one browser
tab's WebSocket. Both are `core.caps.AudioTransport`, and both leave the turn-boundary
logic where it belongs: `core.audio_core.AudioCoreMixin`. Nothing in here decides
where a turn starts or ends — the browser is a microphone and a speaker, nothing more.

This is NOT registered process-wide, and that is the one thing that changed when the
brain moved onto the Mac (Robin, 2026-08-10). Both transports live in one process now,
so `caps.set_audio_transport` — which takes exactly one implementation — cannot describe
the machine any more. `BrowserLiveSession` pins this one on itself
(`AudioCoreMixin.AUDIO_TRANSPORT`), the menubar keeps PortAudio as the machine default,
and neither surface can reroute the other's audio by starting up.

The two rules that shape this file, both inherited from the mic pump:

  1. **Every mic frame reaches the backend.** `_pump_mic` deliberately never
     `continue`s inside the barge-in hold window, because dropping those frames eats
     the first word of a real interruption. So `feed_mic` hands each inbound frame
     straight to `s._mic_cb` — no VAD, no gate, no ring buffer of our own. The one
     gate is `_mic_cb`'s own per-attempt `_audio_stop`, which is the same gate a
     late PortAudio callback hits on the Mac.
  2. **Playback is bounded, and drops oldest.** `s._out_q` is already bounded with
     drop-oldest (`LiveSession._enqueue_audio`); the player thread here blocks on the
     socket exactly as the Mac player blocks on `stream.write`, so a slow phone
     produces the same "drop the oldest chunk" behaviour rather than unbounded latency.
"""
from __future__ import annotations

import asyncio
import functools
import json
import queue
import threading

from core import caps
from core.realtime_client import SR as OUTPUT_SR

_log = caps.log

# Model audio comes back at 24 kHz on both backends (OpenAI's Realtime PCM rate, and
# what Gemini Live emits). The MIC rate is NOT fixed — it is whatever the active
# backend demands (Gemini 16k, OpenAI 24k) and is read off `s._backend.mic_rate` in
# `start()`, never hardcoded here or in the browser.
OUTPUT_RATE = OUTPUT_SR


class BrowserAudioBridge:
    """One browser tab's audio pipe: the send half of its WebSocket, callable from
    any thread.

    The session runs on its own asyncio loop in its own thread (`LiveSession._run`),
    the player runs on a third thread, and the socket belongs to the app's loop. Every
    send therefore hops threads. Audio waits for completion (that IS the backpressure);
    control frames are fire-and-forget so a slow socket can never stall the session
    loop mid-turn.
    """

    def __init__(self, websocket, loop: asyncio.AbstractEventLoop, session_key: str = ""):
        self._ws = websocket
        self._loop = loop
        self.session_key = session_key
        self.mic_rate: int = 0          # set by the transport once the backend is known
        # The last `audio` frame this tab was sent, so it is sent ONCE per format. The
        # browser reconfigures its capture worklet on every one it receives, which resets
        # the resampler's carry and costs a partial frame of speech — cheap, but not
        # while Robin is mid-sentence.
        self.audio_config: dict | None = None
        self._closed = threading.Event()
        self.audio_frames_out = 0       # counters, for /health and tests
        self.mic_frames_in = 0

    # --- lifecycle ----------------------------------------------------------
    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def close(self) -> None:
        """Stop accepting sends. Does not close the socket — the handler owns that."""
        self._closed.set()

    # --- mic in -------------------------------------------------------------
    def feed_mic(self, session, pcm: bytes) -> None:
        """Hand one inbound PCM frame to the session. No gating, ever — see rule 1."""
        if not pcm:
            return
        self.mic_frames_in += 1
        # Same shape PortAudio calls it with: (indata, frames, time, status).
        session._mic_cb(pcm, len(pcm) // 2, None, None)

    # --- out ----------------------------------------------------------------
    def send_audio(self, chunk: bytes, timeout: float = 10.0) -> bool:
        """Blocking send of one model-audio chunk. False once the pipe is unusable."""
        if self._closed.is_set():
            return False
        try:
            fut = asyncio.run_coroutine_threadsafe(self._ws.send_bytes(chunk), self._loop)
            fut.result(timeout)
            self.audio_frames_out += 1
            return True
        except Exception as e:
            _log(f"browser audio send failed: {e!r}")
            self._closed.set()
            return False

    def send_json(self, obj: dict) -> None:
        """Fire-and-forget control frame. Callable from the session loop, the player
        thread, or the app loop itself."""
        if self._closed.is_set():
            return
        payload = json.dumps(obj)

        async def _send():
            try:
                await self._ws.send_text(payload)
            except Exception as e:
                _log(f"browser control send failed: {e!r}")
                self._closed.set()

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        try:
            if running is self._loop:
                self._loop.create_task(_send())
            else:
                asyncio.run_coroutine_threadsafe(_send(), self._loop)
        except Exception as e:
            _log(f"browser control dispatch failed: {e!r}")
            self._closed.set()


class BrowserAudioTransport(caps.AudioTransport):
    """One stateless transport for every tab; the per-tab pipe hangs off the session as
    `_bridge`.

    Stateless on purpose: one object serves every browser session (and coexists with the
    Mac's PortAudio transport in the same process), so every method reads the bridge from
    the session it was handed rather than holding one of its own.
    """

    def announce(self, s) -> bool:
        """Tell the tab what audio format to capture in. True if a frame was sent.

        Split out of `start` so it can be sent EARLY. The tab drops every captured frame
        until this arrives (it does not know the sample rate to resample to), and `start`
        runs after `LiveSession._configure`, which builds the prompt: kernel persona,
        memory recall, the high-stakes manifest. That measured 2.3 s on the tailnet, and
        a tap-and-talk user's first sentence went into it. The rates are known the moment
        the backend object exists — `mic_rate` is a class attribute of the backend, set
        long before its socket opens — so there is nothing to wait for.

        Idempotent per format: `start` still calls it, and on a reconnect that keeps the
        same backend it sends nothing rather than making the tab rebuild its worklet
        mid-conversation.
        """
        bridge: BrowserAudioBridge | None = getattr(s, "_bridge", None)
        if bridge is None:
            raise RuntimeError("no browser bridge attached to this session")
        # The ACTIVE backend decides the mic rate. Gemini wants 16k, OpenAI 24k; the
        # browser is told which, and configures its capture worklet from this frame.
        # 100 ms frames, matching mac.audio's `mic_block = mic_rate // 10`.
        mic_rate = int(s._backend.mic_rate)
        frame = {
            "type": "audio",
            "mic_rate": mic_rate,
            "output_rate": OUTPUT_RATE,
            "frame_samples": mic_rate // 10,
            "manual_vad": bool(s._backend.manual_vad),
        }
        if bridge.audio_config == frame:
            return False
        bridge.mic_rate = mic_rate
        bridge.audio_config = frame
        bridge.send_json(frame)
        _log(f"browser audio: mic {mic_rate} Hz, playback {OUTPUT_RATE} Hz, "
             f"manual_vad={s._backend.manual_vad}")
        return True

    def start(self, s) -> None:
        bridge: BrowserAudioBridge | None = getattr(s, "_bridge", None)
        if bridge is None:
            raise RuntimeError("no browser bridge attached to this session")
        s._audio_stop.clear()
        self.announce(s)
        s._player_thread = threading.Thread(
            target=functools.partial(self.player, s), daemon=True,
            name=f"browser-player-{bridge.session_key[:8]}")
        s._player_thread.start()

    def player(self, s) -> None:
        """Drain `s._out_q` down the socket until this attempt is stopped.

        Mirrors mac.audio's player: exit on the per-attempt `_audio_stop` (not
        `_running`, which stays True across a reconnect), and never write after it.
        """
        bridge: BrowserAudioBridge = s._bridge
        while s._running and not s._audio_stop.is_set():
            try:
                chunk = s._out_q.get(timeout=0.1)
            except queue.Empty:
                continue
            if not chunk or s._audio_stop.is_set():
                continue
            if not bridge.send_audio(chunk):
                _log("browser player: socket gone — stopping playback")
                return

    def teardown(self, s) -> None:
        """Bring this attempt to rest before the next one starts.

        There are no device handles to free here, so the ordering hazard that made the
        Mac teardown delicate does not exist — but the player thread must still be
        JOINED, or a reconnect ends up with two threads writing to one socket and the
        browser hears interleaved audio from two attempts.
        """
        s._audio_stop.set()
        t, s._player_thread = s._player_thread, None
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=3.0)
            if t.is_alive():
                s._audio_dirty = True
                _log("browser player thread still alive after 3s — audio marked dirty")
        # The Mac transport closes the session's shell here; keep that contract so a
        # reconnect does not leak one per attempt.
        if getattr(s, "_shell", None) is not None:
            try:
                s._shell.close()
            except Exception as e:
                _log(f"shell close failed: {e!r}")
            s._shell = None


_TRANSPORT = BrowserAudioTransport()


def transport() -> BrowserAudioTransport:
    """The browser transport. Pinned onto `BrowserLiveSession`, never installed into
    `core.caps` — see the module docstring: the Mac's own transport lives there."""
    return _TRANSPORT
