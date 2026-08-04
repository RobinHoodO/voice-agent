"""Audio I/O for a live session: mic capture → ws pump, and model audio → speaker.

Extracted from realtime.py as AudioMixin — LiveSession mixes it in, so these methods
share the session's state (self._mic_q/_out_q/_in_stream/_out_stream/_loop/level/...).
Kept as a mixin (not a standalone collaborator) so the move is behavior-preserving.
"""
import queue
import threading
import time

from realtime_client import SR

BLOCK = 2400        # mic frames per callback = 100ms at 24kHz
# ponytail: pin live audio by device-name substring. Using a BT headset as the MIC
# forces macOS into low-quality HFP/call mode (quiet playback), so by default capture
# from the built-in mic and play to a headset if present. config.audio overrides these.
MIC_NAME = "MacBook"      # default input: built-in MacBook mic
OUT_NAME = "OpenComm"     # default output preference: Shokz headset (else system default)


def _log(msg: str) -> None:
    try:
        from agent import LOG
        LOG(msg)
    except Exception:
        pass


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


class AudioMixin:
    """Audio capture/playback methods for LiveSession. Relies on session attributes
    set in LiveSession.__init__ (_cfg, _mic_q, _out_q, _in_stream, _out_stream,
    _loop, _running, _player_thread, _audio_stop, _audio_dirty, level, _shell,
    _ws, _backend)."""

    # Per-ATTEMPT audio lifecycle, distinct from the per-SESSION _running flag. A
    # reconnect tears audio down and back up while _running stays True, so the streams
    # and the player thread need their own stop signal — see _teardown_audio.
    _audio_stop: threading.Event
    _audio_dirty: bool = False

    async def _pump_mic(self) -> None:
        n = 0
        while self._running:
            try:
                data = await self._mic_q.get()
            except Exception:
                break
            if data is None or self._ws is None:
                continue
            self._update_level(data)   # metering off the PortAudio callback thread
            if self._backend.manual_vad:
                # ponytail: match OpenAI's familiar 700ms turn boundary locally.
                # While the agent is TALKING the bar is higher and the loud input must
                # be sustained: an open-ear/bone-conduction headset leaks her own voice
                # back into the mic, and a single fixed threshold read that as a barge-in
                # — cancelling her mid-word, then restarting the same answer. Real speech
                # clears both; a leak clears neither. Tunable per mic/headset (live.vad).
                speaking = bool(self._speaking)
                bar = self._vad_bar_speaking if speaking else self._vad_bar
                hold = self._vad_hold if speaking else 0.0
                if self.level > bar:
                    if self._local_loud_since is None:
                        self._local_loud_since = self._loop.time()
                    # Never `continue` here — the frame still has to reach the backend,
                    # or the hold window would swallow the first word of a real barge-in.
                    if (not self._local_speaking
                            and self._loop.time() - self._local_loud_since >= hold):
                        self._local_speaking = True
                        self._local_silence_since = None
                        if speaking:
                            # Rare (only real interruptions) and the one number worth
                            # having: if she still cuts herself off, this is the echo
                            # level to raise live.vad.threshold_while_speaking above.
                            _log(f"barge-in accepted at level {self.level:.2f} "
                                 f"(bar {bar:.2f}) — she was speaking")
                        await self._backend.send_activity_start()
                        await self._on_speech_started()
                    elif self._local_speaking:
                        self._local_silence_since = None
                else:
                    self._local_loud_since = None
                    if self._local_speaking:
                        if self._local_silence_since is None:
                            self._local_silence_since = self._loop.time()
                        elif self._loop.time() - self._local_silence_since >= 0.7:
                            self._local_speaking = False
                            self._local_silence_since = None
                            await self._backend.send_activity_end()
                            for extra in self._backend.drain_extra_events():
                                await self._handle_normalized(extra)
                            await self._on_speech_stopped()
            try:
                await self._backend.send_audio_chunk(data)
                n += 1
                if n == 1 or n % 50 == 0:
                    _log(f"mic frames sent: {n}")
            except Exception as e:
                _log(f"mic pump send failed: {e!r}")
                break

    def _start_audio(self) -> None:
        import sounddevice as sd
        self._audio_stop.clear()
        # PortAudio snapshots the device list at init and never refreshes it. This is a
        # long-running menubar daemon, so a headset connected AFTER launch is invisible
        # (and indexes shift as devices come and go) — the headset "isn't recognized"
        # until a full app restart. Re-enumerate per session; both streams are closed
        # by _teardown_audio before we get here. Best-effort: on failure keep the old
        # behaviour rather than losing audio entirely.
        #
        # sd._terminate() frees PortAudio's GLOBAL state. Calling it while any stream or
        # player thread from the previous attempt is still alive is a use-after-free on
        # CoreAudio's IO thread — SIGSEGV in HALC_ProxyIOContext::IOWorkLoop, which is
        # exactly how the app died mid-reconnect on 2026-08-04. _teardown_audio sets
        # _audio_dirty when it could NOT prove everything was joined; skip the re-init
        # then and keep the cached device list rather than gamble on a segfault.
        if self._audio_dirty:
            _log("portaudio re-init skipped — previous audio teardown did not fully join")
        else:
            try:
                sd._terminate()
                sd._initialize()
            except Exception as e:
                _log(f"portaudio re-init failed (using cached device list): {e!r}")
        mic_rate = self._backend.mic_rate
        mic_block = mic_rate // 10
        want_mic = (self._cfg.get("audio") or {}).get("input_device") or MIC_NAME
        mic = _find_device(want_mic, want_input=True)
        try:
            name = sd.query_devices(mic if mic is not None else None, kind="input").get("name")
            _log(f"audio input device: {name!r} (idx={mic}, pinned to {want_mic!r})")
        except Exception as e:
            _log(f"query input device failed: {e!r}")
        try:
            self._in_stream = sd.RawInputStream(
                samplerate=mic_rate, channels=1, dtype="int16",
                blocksize=mic_block, callback=self._mic_cb, device=mic)
            self._in_stream.start()
        except Exception as e:
            # Device lost / in use / permission denied — fall back to the system default
            # (mirrors the player's fallback) rather than killing the session silently.
            _log(f"mic open failed (device={mic}): {e!r} — trying default input")
            try:
                self._in_stream = sd.RawInputStream(
                    samplerate=mic_rate, channels=1, dtype="int16",
                    blocksize=mic_block, callback=self._mic_cb)
                self._in_stream.start()
            except Exception as e2:
                _log(f"mic open failed on default too: {e2!r}")
                raise RuntimeError(f"microphone unavailable: {e2}") from e2
        _log("audio input stream started")
        self._player_thread = threading.Thread(target=self._player, daemon=True)
        self._player_thread.start()

    def _mic_cb(self, indata, frames, t, status) -> None:
        # PortAudio realtime thread — do the MINIMUM here (copy bytes, hand to the loop).
        # numpy RMS metering moved to _pump_mic so heavy work never runs on this thread,
        # where a stall risks input glitches.
        # _audio_stop (not _running) is the gate: on reconnect _running stays True, and a
        # late callback from the CLOSING stream must not push into the next attempt's queue.
        if self._audio_stop.is_set():
            return
        if self._loop and self._running and self._mic_q is not None:
            data = bytes(indata)
            try:
                self._loop.call_soon_threadsafe(self._mic_q.put_nowait, data)
            except Exception:
                pass

    def _update_level(self, data: bytes) -> None:
        """Mic amplitude 0..1 for the wave pill. Runs off the audio callback thread."""
        try:
            import numpy as np
            s = np.frombuffer(data, dtype=np.int16)
            if s.size:
                rms = float(np.sqrt(np.mean(s.astype(np.float32) ** 2)))
                lvl = min(1.0, rms / 4000.0)
                # fast attack, slow decay — feels like it's catching your words
                self.level = lvl if lvl > self.level else self.level * 0.85 + lvl * 0.15
        except Exception:
            pass

    def _player(self) -> None:
        import sounddevice as sd
        want_out = (self._cfg.get("audio") or {}).get("output_device") or OUT_NAME
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
                self._out_stream = stream
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
        # than self._running (which stays True across a reconnect — the old player used
        # to outlive its own streams and keep writing while teardown freed them).
        try:
            while self._running and not self._audio_stop.is_set():
                try:
                    chunk = self._out_q.get(timeout=0.1)
                except queue.Empty:
                    continue
                if not chunk or self._audio_stop.is_set():
                    continue
                try:
                    stream.write(chunk)
                except Exception as e:
                    _log(f"player write: {e!r}")
        finally:
            # Whoever opened the stream closes it, on this thread, after the last write.
            # Teardown only nulls the reference; closing here means no other thread can
            # ever free it out from under an in-flight write.
            self._out_stream = None
            try:
                stream.stop()
            except Exception as e:
                _log(f"player stream stop: {e!r}")
            try:
                stream.close()
            except Exception as e:
                _log(f"player stream close: {e!r}")

    def _flush_out(self) -> None:
        # Barge-in: drop queued audio. The ~100ms chunk mid-write finishes — close enough.
        try:
            while True:
                self._out_q.get_nowait()
        except queue.Empty:
            pass

    def _teardown_audio(self) -> None:
        """Bring this attempt's audio fully to rest before anyone re-inits PortAudio.

        Order matters and is the whole point: signal first, then close the input stream,
        then JOIN the player (which closes its own output stream on the way out). Closing
        a stream while its PortAudio callback is in flight — or letting a stray player
        thread survive into the next attempt's sd._terminate() — is a use-after-free on
        CoreAudio's IO thread. Sets _audio_dirty if it can't prove the player exited, so
        _start_audio knows not to re-init PortAudio underneath it."""
        self._audio_stop.set()
        # Null the reference BEFORE touching the object: _mic_cb reads _in_stream-adjacent
        # state and must see "gone" rather than "closing".
        s, self._in_stream = self._in_stream, None
        if s is not None:
            try:
                s.stop()
            except Exception as e:
                _log(f"mic stream stop: {e!r}")
            try:
                s.close()
            except Exception as e:
                _log(f"mic stream close: {e!r}")
        t, self._player_thread = self._player_thread, None
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=3.0)
            if t.is_alive():
                self._audio_dirty = True
                _log("player thread still alive after 3s — audio marked dirty")
        if self._out_stream is not None:
            # The player normally closes this itself; only reachable if it never started.
            s, self._out_stream = self._out_stream, None
            try:
                s.stop()
                s.close()
            except Exception as e:
                _log(f"output stream close: {e!r}")
        if self._shell is not None:
            self._shell.close()
            self._shell = None
