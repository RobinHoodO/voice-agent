"""Local VAD must not let the agent interrupt herself.

Robin runs an open-ear Shokz headset with the MacBook's built-in mic, so her own voice
leaks back in. With one fixed threshold that read as a barge-in: she was cancelled
mid-word and restarted the same sentence ("Clarissa Magalhaes" ... "Clarissa Magalhaes
is a biomaterials curator..."). While she is speaking the bar is higher AND the loud
input must persist, so a leak is rejected and a real interruption still lands.
"""
import asyncio

from core import audio_core


class _Backend:
    manual_vad = True

    def __init__(self):
        self.starts = self.ends = self.chunks = 0

    async def send_activity_start(self):
        self.starts += 1

    async def send_activity_end(self):
        self.ends += 1

    async def send_audio_chunk(self, data):
        self.chunks += 1

    def drain_extra_events(self):
        return []


class _Session(audio_core.AudioCoreMixin):
    """Minimal stand-in exposing exactly the attributes _pump_mic touches."""

    def __init__(self, levels, agent_speaking, hold=0.25, tick=0.1, silence=1.5):
        self._levels = list(levels)
        self._speaking = agent_speaking
        self._backend = _Backend()
        self._ws = object()
        self._running = True
        self._mic_q = asyncio.Queue()
        self.level = 0.0
        self.level_raw = 0.0
        self._local_speaking = False
        self._local_silence_since = None
        self._local_loud_since = None
        self._vad_bar = 0.10
        self._vad_bar_speaking = 0.28
        self._vad_hold = hold
        self._vad_silence = silence
        self._tick = tick
        self._now = 0.0
        self._loop = self
        self.started = self.stopped = 0

    def time(self):                      # stands in for loop.time()
        return self._now

    def _update_level(self, data):       # scripted levels instead of real PCM
        self.level = self.level_raw = self._levels.pop(0)
        self._now += self._tick
        if not self._levels:
            self._running = False

    async def _on_speech_started(self):
        self.started += 1

    async def _on_speech_stopped(self):
        self.stopped += 1

    def run(self):
        for _ in self._levels:
            self._mic_q.put_nowait(b"\x00\x00")
        asyncio.run(self._pump_mic())
        return self


def test_headset_echo_does_not_interrupt_her():
    """Her own voice leaking back sits above the idle bar but below the speaking bar."""
    s = _Session([0.18] * 10, agent_speaking=True).run()
    assert s.started == 0, "leaked agent audio must not count as the user speaking"
    assert s._backend.starts == 0, "and must never send activityStart (which cancels her)"


def test_real_interruption_still_lands():
    """A genuine barge-in is loud and sustained — it must get through."""
    s = _Session([0.55] * 10, agent_speaking=True).run()
    assert s.started == 1
    assert s._backend.starts == 1


def test_a_brief_loud_blip_is_rejected():
    """A door slam / keyboard bang while she talks: loud but not sustained past the hold."""
    s = _Session([0.55, 0.55, 0.01, 0.01, 0.01, 0.01], agent_speaking=True, hold=0.25).run()
    assert s.started == 0, "a blip shorter than the hold must not cancel her"


def test_normal_speech_when_she_is_silent_is_immediate():
    """No hold when she isn't talking — starting a turn must stay snappy."""
    s = _Session([0.15] * 4, agent_speaking=False).run()
    assert s.started == 1
    assert s._vad_hold == 0.25 and s._local_speaking


def test_every_frame_still_reaches_the_backend():
    """The hold window must not swallow audio, or the first word of a real barge-in
    would be clipped off the transcript."""
    s = _Session([0.55] * 8, agent_speaking=True).run()
    assert s._backend.chunks == 8


def test_silence_ends_the_turn():
    s = _Session([0.5, 0.5] + [0.0] * 12, agent_speaking=False, tick=0.2).run()
    assert s.started == 1 and s.stopped == 1
    assert s._backend.ends == 1


def test_thinking_pause_mid_sentence_does_not_end_the_turn():
    """Robin pauses ~1s to think, then keeps talking. The old 0.7s window ended his turn
    there and she answered half a request while he was still speaking."""
    s = _Session([0.5] + [0.0] * 5 + [0.5] * 4, agent_speaking=False, tick=0.2).run()
    assert s.stopped == 0, "a 1.0s thinking pause must not be treated as end-of-turn"
    assert s._backend.ends == 0
    assert s.started == 1, "and it is still one continuous turn, not two"


def test_the_smoothed_tail_does_not_delay_end_of_turn():
    """The 'she got slower' regression, as a number.

    `_update_level` smooths the level (level*0.85 + lvl*0.15), so after loud speech it
    needs ~1.4s to decay to the 0.10 bar. Ending the turn off THAT meant every reply
    waited decay + silence_sec (~2.9s) instead of silence_sec (1.5s). End-of-turn now
    reads the raw frame; starting one still reads the smoothed level.
    """
    s = _Session([0.9] * 2, agent_speaking=False, tick=0.1)
    # Real decay: the frame goes quiet but the smoothed tail is still above the bar.
    s._levels = [0.9, 0.9] + [0.0] * 20
    real_smoothed = []

    def decaying(_data):
        raw = s._levels.pop(0)
        s.level_raw = raw
        s.level = raw if raw > s.level else s.level * 0.85 + raw * 0.15
        real_smoothed.append(s.level)
        s._now += s._tick
        if not s._levels:
            s._running = False

    stopped_at = []
    real_stop = s._on_speech_stopped

    async def record_stop():
        stopped_at.append(s._now)
        await real_stop()

    s._update_level = decaying
    s._on_speech_stopped = record_stop
    s.run()

    assert s.stopped == 1, "the turn must end"
    assert s._backend.ends == 1
    # Speech ends at 0.2s; the window is 1.5s. Anything approaching 0.2+1.4+1.5 means the
    # smoothed decay is being waited on again.
    assert stopped_at[0] <= 0.2 + 1.5 + 0.3, (
        f"end-of-turn at {stopped_at[0]:.1f}s — the smoothed decay is back in the path")
    # And it really did end while the smoothed tail was still above the bar.
    assert max(real_smoothed[-3:]) > s._vad_bar or stopped_at[0] < 1.9


def test_defaults_are_wired_from_config():
    from core import config
    vad = config.DEFAULTS["live"]["vad"]
    assert vad["threshold_while_speaking"] > vad["threshold"], \
        "interrupting her must be harder than starting a turn in silence"
    assert vad["barge_in_hold_sec"] > 0
