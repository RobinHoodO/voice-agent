"""macOS audio transport: PortAudio device enumeration, capture + playback streams.

This is the machine half of the old `audio.py`. The turn-boundary half — the manual
VAD in `_pump_mic`, the capture callback, metering, the barge-in flush — moved to
`core.audio_core.AudioCoreMixin` untouched, so a browser-mic transport reuses it.

Everything here operates on a session object `s` (the LiveSession) rather than `self`,
which is what makes it swappable: `install()` registers it as the process-wide
`core.caps.AudioTransport`. `AudioMixin` remains as a thin mixin over the same code for
callers that want the methods bound to a session (and for the teardown regression tests).
"""
import functools
import queue
import threading
import time

from core import caps
from core.audio_core import AudioCoreMixin
from core.realtime_client import SR

_log = caps.log

BLOCK = 2400        # mic frames per callback = 100ms at 24kHz
# ponytail: pin live audio by device-name substring. Using a BT headset as the MIC
# forces macOS into low-quality HFP/call mode (quiet playback), so by default capture
# from the built-in mic and play to a headset if present. config.audio overrides these.
MIC_NAME = "MacBook"      # default input: built-in MacBook mic
OUT_NAME = "OpenComm"     # default output preference: Shokz headset (else system default)


def _find_device(substr: str, want_input: bool):
    """Index of the first device whose name contains substr (case-insensitive) and has
    the right direction; None (= system default) if not found."""
    import sounddevice as sd
    try:
        for i, d in enumerate(sd.query_devices()):
            ch = d["max_input_channels"] if want_input else d["max_output_channels"]
            if ch > 0 and substr.lower() in d["name"].lower():
                return i
    except Exception as e:
        _log(f"device lookup failed: {e!r}")
    return None


class MacAudioTransport(caps.AudioTransport):
    """PortAudio streams for one session attempt."""

    def start(self, s) -> None:
        import sounddevice as sd
        s._audio_stop.clear()
        # PortAudio snapshots the device list at init and never refreshes it. This is a
        # long-running menubar daemon, so a headset connected AFTER launch is invisible
        # (and indexes shift as devices come and go) — the headset "isn't recognized"
        # until a full app restart. Re-enumerate per session; both streams are closed
        # by teardown before we get here. Best-effort: on failure keep the old
        # behaviour rather than losing audio entirely.
        #
        # sd._terminate() frees PortAudio's GLOBAL state. Calling it while any stream or
        # player thread from the previous attempt is still alive is a use-after-free on
        # CoreAudio's IO thread — SIGSEGV in HALC_ProxyIOContext::IOWorkLoop, which is
        # exactly how the app died mid-reconnect on 2026-08-04. teardown sets
        # _audio_dirty when it could NOT prove everything was joined; skip the re-init
        # then and keep the cached device list rather than gamble on a segfault.
        if s._audio_dirty:
            _log("portaudio re-init skipped — previous audio teardown did not fully join")
        else:
            try:
                sd._terminate()
                sd._initialize()
            except Exception as e:
                _log(f"portaudio re-init failed (using cached device list): {e!r}")
        mic_rate = s._backend.mic_rate
        mic_block = mic_rate // 10
        want_mic = (s._cfg.get("audio") or {}).get("input_device") or MIC_NAME
        mic = _find_device(want_mic, want_input=True)
        try:
            name = sd.query_devices(mic if mic is not None else None, kind="input").get("name")
            _log(f"audio input device: {name!r} (idx={mic}, pinned to {want_mic!r})")
        except Exception as e:
            _log(f"query input device failed: {e!r}")
        try:
            s._in_stream = sd.RawInputStream(
                samplerate=mic_rate, channels=1, dtype="int16",
                blocksize=mic_block, callback=s._mic_cb, device=mic)
            s._in_stream.start()
        except Exception as e:
            # Device lost / in use / permission denied — fall back to the system default
            # (mirrors the player's fallback) rather than killing the session silently.
            _log(f"mic open failed (device={mic}): {e!r} — trying default input")
            try:
                s._in_stream = sd.RawInputStream(
                    samplerate=mic_rate, channels=1, dtype="int16",
                    blocksize=mic_block, callback=s._mic_cb)
                s._in_stream.start()
            except Exception as e2:
                _log(f"mic open failed on default too: {e2!r}")
                raise RuntimeError(f"microphone unavailable: {e2}") from e2
        _log("audio input stream started")
        s._player_thread = threading.Thread(target=functools.partial(self.player, s),
                                            daemon=True)
        s._player_thread.start()

    def player(self, s) -> None:
        import sounddevice as sd
        want_out = (s._cfg.get("audio") or {}).get("output_device") or OUT_NAME
        out = _find_device(want_out, want_input=False)
        try:
            name = sd.query_devices(out if out is not None else None, kind="output").get("name")
            _log(f"audio output device: {name!r} (idx={out}, prefer {want_out!r})")
        except Exception as e:
            _log(f"query output device failed: {e!r}")
        # ponytail: Bluetooth headsets (Shokz) often fail RawOutputStream with PortAudio
        # -9986 on first open — the SCO link isn't ready. Old code fell back to the system
        # *default*, which IS the same headset, so it failed twice and went silent. Try the
        # preferred device, retry once after a warmup, then the built-in speakers (a DIFFERENT
        # device), then default. First that opens wins.
        builtin = _find_device("MacBook", want_input=False)
        attempts = [("preferred", out), ("preferred-retry", out),
                    ("built-in", builtin), ("default", None)]
        stream = None
        for label, dev in attempts:
            if label == "built-in" and (dev is None or dev == out):
                continue   # no distinct built-in to fall back to
            if label == "preferred-retry":
                time.sleep(0.4)   # let a flaky BT output settle before the second try
            try:
                stream = sd.RawOutputStream(samplerate=SR, channels=1, dtype="int16", device=dev)
                stream.start()
                s._out_stream = stream
                if label != "preferred":
                    _log(f"player using {label} output (idx={dev})")
                break
            except Exception as e:
                stream = None
                _log(f"player init failed ({label}): {e!r}")
        else:
            _log("player gave up — no usable output device")
            return
        # Own the stream through a LOCAL, and exit on the per-attempt stop event rather
        # than s._running (which stays True across a reconnect — the old player used
        # to outlive its own streams and keep writing while teardown freed them).
        try:
            while s._running and not s._audio_stop.is_set():
                try:
                    chunk = s._out_q.get(timeout=0.1)
                except queue.Empty:
                    continue
                if not chunk or s._audio_stop.is_set():
                    continue
                try:
                    stream.write(chunk)
                except Exception as e:
                    _log(f"player write: {e!r}")
        finally:
            # Whoever opened the stream closes it, on this thread, after the last write.
            # Teardown only nulls the reference; closing here means no other thread can
            # ever free it out from under an in-flight write.
            s._out_stream = None
            try:
                stream.stop()
            except Exception as e:
                _log(f"player stream stop: {e!r}")
            try:
                stream.close()
            except Exception as e:
                _log(f"player stream close: {e!r}")

    def teardown(self, s) -> None:
        """Bring this attempt's audio fully to rest before anyone re-inits PortAudio.

        Order matters and is the whole point: signal first, then close the input stream,
        then JOIN the player (which closes its own output stream on the way out). Closing
        a stream while its PortAudio callback is in flight — or letting a stray player
        thread survive into the next attempt's sd._terminate() — is a use-after-free on
        CoreAudio's IO thread. Sets _audio_dirty if it can't prove the player exited, so
        start() knows not to re-init PortAudio underneath it."""
        s._audio_stop.set()
        # Null the reference BEFORE touching the object: _mic_cb reads _in_stream-adjacent
        # state and must see "gone" rather than "closing".
        st, s._in_stream = s._in_stream, None
        if st is not None:
            try:
                st.stop()
            except Exception as e:
                _log(f"mic stream stop: {e!r}")
            try:
                st.close()
            except Exception as e:
                _log(f"mic stream close: {e!r}")
        t, s._player_thread = s._player_thread, None
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=3.0)
            if t.is_alive():
                s._audio_dirty = True
                _log("player thread still alive after 3s — audio marked dirty")
        if s._out_stream is not None:
            # The player normally closes this itself; only reachable if it never started.
            st, s._out_stream = s._out_stream, None
            try:
                st.stop()
                st.close()
            except Exception as e:
                _log(f"output stream close: {e!r}")
        if s._shell is not None:
            s._shell.close()
            s._shell = None


_TRANSPORT = MacAudioTransport()


class AudioMixin(AudioCoreMixin):
    """The old mixin surface: core turn logic + the macOS transport bound to `self`."""

    def _start_audio(self) -> None:
        _TRANSPORT.start(self)

    def _player(self) -> None:
        _TRANSPORT.player(self)

    def _teardown_audio(self) -> None:
        _TRANSPORT.teardown(self)


def install() -> None:
    caps.set_audio_transport(_TRANSPORT)
