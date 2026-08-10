"""Transport-independent audio for a live session: the turn-boundary logic.

Split out of the old `audio.py` AudioMixin. What lives here is everything a *browser
mic* or a *SIP leg* would need exactly as-is:

  * `_pump_mic`  — drains the session's mic queue and, on manual-VAD backends, decides
                   where a turn starts and ends (live.vad.*). The numbers and their
                   semantics are byte-for-byte what shipped: a higher bar plus a
                   sustained hold to interrupt her, an immediate start when she's
                   silent, and a silence timer to end the turn.
  * `_mic_cb`    — the capture callback. Pure Python: it copies bytes onto the loop and
                   is gated by the per-attempt `_audio_stop`, nothing device-specific.
  * `_update_level` / `_flush_out` — metering and barge-in drop.

What is NOT here, because it is a real machine: device enumeration, PortAudio streams,
the player thread. Those are `core.caps.AudioTransport`; `mac.audio` implements it.
"""
import queue
import threading

from core import caps

_log = caps.log


class AudioCoreMixin:
    """Audio methods for LiveSession that carry no device dependency. Relies on session
    attributes set in LiveSession.__init__ (_cfg, _mic_q, _out_q, _in_stream,
    _out_stream, _loop, _running, _player_thread, _audio_stop, _audio_dirty, level,
    _shell, _ws, _backend)."""

    # Per-ATTEMPT audio lifecycle, distinct from the per-SESSION _running flag. A
    # reconnect tears audio down and back up while _running stays True, so the streams
    # and the player thread need their own stop signal — see _teardown_audio.
    _audio_stop: threading.Event
    _audio_dirty: bool = False

    # Which transport owns THIS session's microphone and speaker. None = whatever the
    # process registered in `core.caps`.
    #
    # It has to be per-session now, and that is a consequence of Robin's 2026-08-10
    # ruling rather than a nicety: ONE process on his Mac holds both surfaces — the
    # menubar conversation on PortAudio and a phone conversation on a WebSocket. A
    # process-wide `caps.set_audio_transport` cannot describe that, and a surface that
    # swapped it at startup would silently reroute the OTHER surface's audio. So the
    # registration in `core.caps` is the DEFAULT (what a machine has when nobody says
    # otherwise) and a session class that brings its own says so here — exactly the
    # shape `PROFILE` already uses for capabilities.
    AUDIO_TRANSPORT = None

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
                # ponytail: end-of-turn is a local silence timer (live.vad.silence_sec).
                # While the agent is TALKING the bar is higher and the loud input must
                # be sustained: an open-ear/bone-conduction headset leaks her own voice
                # back into the mic, and a single fixed threshold read that as a barge-in
                # — cancelling her mid-word, then restarting the same answer. Real speech
                # clears both; a leak clears neither. Tunable per mic/headset (live.vad).
                speaking = bool(self._speaking)
                bar = self._vad_bar_speaking if speaking else self._vad_bar
                hold = self._vad_hold if speaking else 0.0
                silence = self._vad_silence
                # STARTING a turn (and barge-in) reads the SMOOTHED level: its slow decay
                # is what bridges the gaps between words so a real interruption sustains
                # past the hold. ENDING one reads the RAW frame, because the decay is pure
                # lag there — after loud speech the smoothed level needs ~1.4 s to fall to
                # the bar before the silence timer would even start, on top of silence_sec.
                # That is what made her feel slow: ~2.9 s to answer against a 1.5 s window.
                quiet_now = self.level_raw <= bar
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
                    elif self._local_speaking and not quiet_now:
                        # Still actually making sound — the turn is alive. If the frame is
                        # quiet and only the smoothed tail is above the bar, fall through
                        # so the silence timer below can start now rather than in 1.4 s.
                        self._local_silence_since = None
                if quiet_now:
                    self._local_loud_since = None
                    if self._local_speaking:
                        if self._local_silence_since is None:
                            self._local_silence_since = self._loop.time()
                        elif self._loop.time() - self._local_silence_since >= silence:
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
                # THIS frame, unsmoothed. `_pump_mic` ends a turn off this, because the
                # smoothed level below decays from 1.0 to the 0.10 bar in ~1.4 s and that
                # delay lands on every single reply. Robin reported her "slower" on
                # 2026-08-10 and this was the reason: ~2.9 s to start answering against a
                # 1.5 s configured window.
                self.level_raw = lvl
                # fast attack, slow decay — feels like it's catching your words, and the
                # decay is deliberately kept for STARTING a turn: it bridges the gaps
                # between words so a real barge-in sustains past the 0.25 s hold instead
                # of flickering under the bar mid-sentence.
                self.level = lvl if lvl > self.level else self.level * 0.85 + lvl * 0.15
        except Exception:
            pass

    def _flush_out(self) -> None:
        # Barge-in: drop queued audio. The ~100ms chunk mid-write finishes — close enough.
        try:
            while True:
                self._out_q.get_nowait()
        except queue.Empty:
            pass

    # --- transport hooks ----------------------------------------------------
    # The surface owns devices and streams; see core.caps.AudioTransport.
    def _transport(self):
        """This session's transport: its own if it pinned one, else the machine's.

        Resolved per call rather than snapshotted, for the same reason `profile_name` is:
        a session can be constructed before the surface has finished installing.
        """
        return self.AUDIO_TRANSPORT or caps.audio_transport()

    def _start_audio(self) -> None:
        self._transport().start(self)

    def _player(self) -> None:
        self._transport().player(self)

    def _teardown_audio(self) -> None:
        self._transport().teardown(self)
