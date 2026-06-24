"""Audio I/O for a live session: mic capture → ws pump, and model audio → speaker.

Extracted from realtime.py as AudioMixin — LiveSession mixes it in, so these methods
share the session's state (self._mic_q/_out_q/_in_stream/_out_stream/_loop/level/...).
Kept as a mixin (not a standalone collaborator) so the move is behavior-preserving.
"""
import base64
import json
import queue
import threading

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
    _loop, _running, _player_thread, level, _shell, _ws)."""

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
            try:
                await self._ws.send(json.dumps({
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(data).decode(),
                }))
                n += 1
                if n == 1 or n % 50 == 0:
                    _log(f"mic frames sent: {n}")
            except Exception as e:
                _log(f"mic pump send failed: {e!r}")
                break

    def _start_audio(self) -> None:
        import sounddevice as sd
        want_mic = (self._cfg.get("audio") or {}).get("input_device") or MIC_NAME
        mic = _find_device(want_mic, want_input=True)
        try:
            name = sd.query_devices(mic if mic is not None else None, kind="input").get("name")
            _log(f"audio input device: {name!r} (idx={mic}, pinned to {want_mic!r})")
        except Exception as e:
            _log(f"query input device failed: {e!r}")
        try:
            self._in_stream = sd.RawInputStream(
                samplerate=SR, channels=1, dtype="int16",
                blocksize=BLOCK, callback=self._mic_cb, device=mic)
            self._in_stream.start()
        except Exception as e:
            # Device lost / in use / permission denied — fall back to the system default
            # (mirrors the player's fallback) rather than killing the session silently.
            _log(f"mic open failed (device={mic}): {e!r} — trying default input")
            try:
                self._in_stream = sd.RawInputStream(
                    samplerate=SR, channels=1, dtype="int16",
                    blocksize=BLOCK, callback=self._mic_cb)
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
        try:
            self._out_stream = sd.RawOutputStream(samplerate=SR, channels=1, dtype="int16", device=out)
            self._out_stream.start()
        except Exception as e:
            _log(f"player init failed: {e!r} — falling back to default output")
            try:
                self._out_stream = sd.RawOutputStream(samplerate=SR, channels=1, dtype="int16")
                self._out_stream.start()
            except Exception as e2:
                _log(f"player default init also failed: {e2!r}")
                return
        while self._running:
            try:
                chunk = self._out_q.get(timeout=0.1)
            except queue.Empty:
                continue
            if not chunk:
                continue
            try:
                self._out_stream.write(chunk)
            except Exception as e:
                _log(f"player write: {e!r}")

    def _flush_out(self) -> None:
        # Barge-in: drop queued audio. The ~100ms chunk mid-write finishes — close enough.
        try:
            while True:
                self._out_q.get_nowait()
        except queue.Empty:
            pass

    def _teardown_audio(self) -> None:
        for s in (self._in_stream, self._out_stream):
            try:
                if s:
                    s.stop()
                    s.close()
            except Exception:
                pass
        self._in_stream = self._out_stream = None
        if self._shell is not None:
            self._shell.close()
            self._shell = None
