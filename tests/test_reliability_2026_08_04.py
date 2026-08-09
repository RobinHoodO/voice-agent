"""Regression tests for the 2026-08-04 crash: Pam segfaulted mid-reconnect and took an
unsaved conversation with her, after 21s of silent duplicate tool calls and 72s of no
response at all. Four independent failures, one test each.

Diagnosis in ARCHITECTURE.md ("Failure modes"). Run: pytest tests/test_reliability_2026_08_04.py
"""
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import memory
from mac.audio import AudioMixin
from core.live_session import (TOOL_REPEAT_LIMIT, TOOL_REPEAT_WINDOW_S,
                          _tool_repeat_count)


# --- 1. loop guard ----------------------------------------------------------
def test_identical_calls_trip_the_guard_and_different_args_do_not():
    recent, now = [], 1000.0
    args = {"subject": "Borderland"}
    counts = [_tool_repeat_count(recent, "focus", args, now + i) for i in range(5)]
    assert counts == [0, 1, 2, 3, 4], counts
    # The real loop fired 16x; the guard refuses from the 3rd on.
    assert sum(c >= TOOL_REPEAT_LIMIT for c in counts) == 2

    # A different subject is a different call — must not be throttled.
    assert _tool_repeat_count(recent, "focus", {"subject": "Mingle"}, now + 5) == 0
    # Same args, different tool — also independent.
    assert _tool_repeat_count(recent, "semsearch_query", args, now + 5) == 0


def test_guard_window_expires_so_a_later_retry_is_allowed():
    recent, now = [], 1000.0
    last = now
    for i in range(TOOL_REPEAT_LIMIT + 2):
        last = now + i
        _tool_repeat_count(recent, "focus", {"subject": "x"}, last)
    # The window is measured from the NEWEST entry, so clear all of them before retrying.
    assert _tool_repeat_count(
        recent, "focus", {"subject": "x"}, last + TOOL_REPEAT_WINDOW_S + 1) == 0


def test_key_is_stable_across_dict_ordering():
    recent, now = [], 1000.0
    assert _tool_repeat_count(recent, "t", {"a": 1, "b": 2}, now) == 0
    assert _tool_repeat_count(recent, "t", {"b": 2, "a": 1}, now) == 1


# --- 2. crash-safe transcript journal ---------------------------------------
def test_journal_survives_a_process_that_never_reaches_persist(tmp_path):
    memory.DB_PATH = str(tmp_path / "conv.db")
    memory.JOURNAL_PATH = str(tmp_path / "turns.journal")
    memory._schema_done.clear()

    # Session records turns, then dies. No _persist_conversation, no `finally`.
    memory.journal_append("you: I have an idea about the Jomo guide")
    memory.journal_append("agent: go on")
    assert os.path.exists(memory.JOURNAL_PATH)

    cid = memory.journal_recover()
    assert cid, "journal recovery produced no conversation"
    # Assert on the stored transcript, not recall() — recall summarizes and also folds in
    # the external provider, so it would pass on the summary alone. The WORDS are the point.
    conn = memory._db()
    try:
        stored = conn.execute(
            "SELECT transcript FROM conversations WHERE id=?", (cid,)).fetchone()[0]
    finally:
        conn.close()
    assert "I have an idea about the Jomo guide" in stored
    assert "agent: go on" in stored
    assert not os.path.exists(memory.JOURNAL_PATH), "journal not cleared after recovery"
    assert memory.journal_recover() == 0, "second recovery should be a no-op"


def test_journal_clear_leaves_nothing_to_recover(tmp_path):
    memory.DB_PATH = str(tmp_path / "conv.db")
    memory.JOURNAL_PATH = str(tmp_path / "turns.journal")
    memory._schema_done.clear()
    memory.journal_append("you: stored normally")
    memory.journal_clear()
    assert memory.journal_recover() == 0


# --- 3. audio teardown race (the segfault) ----------------------------------
class _FakeStream:
    """Minimal stand-in that records lifecycle order and refuses use-after-close —
    the C-level equivalent of which is the SIGSEGV this guards against."""

    def __init__(self, events, name):
        self.events, self.name, self.closed = events, name, False

    def stop(self):
        self.events.append(f"{self.name}.stop")

    def close(self):
        self.events.append(f"{self.name}.close")
        self.closed = True

    def write(self, chunk):
        if self.closed:
            raise AssertionError("write after close — this is the segfault")


class _Session(AudioMixin):
    def __init__(self):
        self._audio_stop = threading.Event()
        self._audio_dirty = False
        self._running = True
        self._in_stream = self._out_stream = None
        self._player_thread = None
        self._shell = None
        self._loop = None
        self._mic_q = None
        self.events = []


def test_teardown_joins_the_player_before_returning():
    s = _Session()
    s._in_stream = _FakeStream(s.events, "mic")
    out = _FakeStream(s.events, "out")
    s._out_stream = out
    started = threading.Event()

    def player():
        started.set()
        while s._running and not s._audio_stop.is_set():
            time.sleep(0.01)
        s.events.append("player.exit")
        s._out_stream = None
        out.stop()
        out.close()

    s._player_thread = threading.Thread(target=player, daemon=True)
    s._player_thread.start()
    started.wait(2.0)

    s._teardown_audio()

    assert not s._audio_dirty, "clean teardown must not mark audio dirty"
    assert "player.exit" in s.events, "teardown returned before the player exited"
    assert s._in_stream is None and s._out_stream is None
    assert out.closed, "output stream left open"


def test_teardown_marks_dirty_when_the_player_will_not_die():
    """A wedged player must block the next attempt's sd._terminate(), not race it."""
    s = _Session()
    stuck = threading.Event()
    s._player_thread = threading.Thread(target=lambda: stuck.wait(30), daemon=True)
    s._player_thread.start()
    try:
        t0 = time.time()
        s._teardown_audio()
        assert s._audio_dirty, "wedged player must set _audio_dirty"
        assert time.time() - t0 < 6, "teardown must bound its join, not hang forever"
    finally:
        stuck.set()


def test_mic_callback_is_silenced_by_the_stop_event():
    """A late callback from the CLOSING stream must not feed the next attempt's queue."""
    s = _Session()
    delivered = []
    s._loop = type("L", (), {"call_soon_threadsafe": lambda self, fn, d: delivered.append(d)})()
    s._mic_q = type("Q", (), {"put_nowait": lambda self, d: None})()

    s._mic_cb(b"\x00\x01", 1, None, None)
    assert len(delivered) == 1, "callback should deliver while live"

    s._audio_stop.set()
    s._mic_cb(b"\x00\x01", 1, None, None)
    assert len(delivered) == 1, "callback delivered after stop — teardown race is open"


# --- 4. stall watchdog (the 72 silent seconds) ------------------------------
class _FakeBackend:
    resume_handle = None

    def __init__(self):
        self.contexts, self.triggers, self.closed = [], 0, False

    async def send_text_context(self, text, image_b64=None):
        self.contexts.append(text)

    async def trigger_response(self):
        self.triggers += 1

    async def close(self):
        self.closed = True


def _run_stall(waited_pattern, cfg=None):
    """Drive _stall_watchdog against a scripted clock. `waited_pattern` is the sequence of
    'seconds since the turn went silent' the watchdog observes, one per tick."""
    import asyncio

    from core import live_session as ls

    s = ls.LiveSession.__new__(ls.LiveSession)
    s._running = True
    s._cfg = cfg or {"live": {"stall_tone_s": 6, "stall_nudge_s": 15,
                              "stall_reconnect_s": 35}}
    s._backend = _FakeBackend()
    s.on_state = lambda _st: None
    s._notify = lambda _m: None
    s.earcons = []
    s._earcon = lambda: s.earcons.append(1)
    s._awaiting_reply_since = 0.0

    ticks = list(waited_pattern)

    class _Loop:
        def time(self_inner):
            return ticks[0] if ticks else 0.0

    s._loop = _Loop()

    async def _fake_sleep(_d):
        ticks.pop(0)
        if not ticks:
            s._running = False

    orig_sleep = asyncio.sleep
    asyncio.sleep = _fake_sleep
    try:
        asyncio.run(s._stall_watchdog())
    finally:
        asyncio.sleep = orig_sleep
    return s


def test_a_short_pause_stays_silent():
    """Normal thinking latency must NOT produce a tone — the cue has to stay meaningful."""
    s = _run_stall([1, 2, 3, 4, 5])
    assert s.earcons == []
    assert s._backend.triggers == 0
    assert not s._backend.closed


def test_a_long_pause_escalates_tone_then_nudge_then_reconnect():
    s = _run_stall([2, 7, 10, 16, 20, 40])
    assert len(s.earcons) == 1, "expected exactly one working tone"
    assert s._backend.triggers == 1, "expected exactly one spoken nudge"
    assert "silent" in s._backend.contexts[0].lower()
    assert s._backend.closed, "a 40s dead turn must drop the socket to reconnect"


def test_each_cue_fires_once_per_stall_not_every_tick():
    s = _run_stall([7, 8, 9, 10, 11, 12, 13, 14])
    assert len(s.earcons) == 1
    assert s._backend.triggers == 0


def test_reply_arriving_disarms_the_watchdog():
    """AUDIO_DELTA clears _awaiting_reply_since; the watchdog must then do nothing."""
    import asyncio

    from core import live_session as ls

    s = ls.LiveSession.__new__(ls.LiveSession)
    s._running = True
    s._cfg = {"live": {"stall_tone_s": 1, "stall_nudge_s": 2, "stall_reconnect_s": 3}}
    s._backend = _FakeBackend()
    s.on_state = lambda _st: None
    s._notify = lambda _m: None
    s.earcons = []
    s._earcon = lambda: s.earcons.append(1)
    s._awaiting_reply_since = None          # she answered
    s._loop = type("L", (), {"time": lambda self: 999.0})()

    n = [0]

    async def _fake_sleep(_d):
        n[0] += 1
        if n[0] >= 5:
            s._running = False

    orig_sleep = asyncio.sleep
    asyncio.sleep = _fake_sleep
    try:
        asyncio.run(s._stall_watchdog())
    finally:
        asyncio.sleep = orig_sleep
    assert s.earcons == [] and s._backend.triggers == 0 and not s._backend.closed


def test_earcon_produces_playable_pcm():
    from core import live_session as ls

    s = ls.LiveSession.__new__(ls.LiveSession)
    chunks = []
    s._enqueue_audio = chunks.append
    s._earcon()
    assert chunks and len(chunks[0]) > 1000, "earcon produced no audio"
    assert len(chunks[0]) % 2 == 0, "not int16-aligned PCM"


# --- 5. silent recovery (16:39 on 2026-08-04) -------------------------------
# The watchdog above worked: it dropped the dead socket and reconnected in one second.
# Then it said nothing, so Robin sat in silence and killed the session a minute later.
def _run_reconnect(resume_handle=None):
    import asyncio

    from core import live_session as ls

    s = ls.LiveSession.__new__(ls.LiveSession)
    s._backend = _FakeBackend()
    s._backend.resume_handle = resume_handle
    s._reconnected = True
    s._awaiting_reply_since = None
    s._loop = type("L", (), {"time": lambda self: 42.0})()
    asyncio.run(s._speak_reconnect())
    return s


def test_reconnect_is_never_silent():
    s = _run_reconnect(resume_handle="h-1")
    assert s._backend.triggers == 1, "reconnected without saying anything"
    assert "say that again" in s._backend.contexts[0].lower()
    assert not s._reconnected, "flag must clear or every turn re-announces"
    assert s._awaiting_reply_since == 42.0, "the recovery line itself must be watched too"


def test_reconnect_without_a_handle_admits_the_thread_is_gone():
    """Resumed, she only lost a sentence. Cold, she lost the conversation — and saying
    'go on' as if she still had it is worse than admitting the gap."""
    resumed = _run_reconnect(resume_handle="h-1")._backend.contexts[0]
    cold = _run_reconnect(resume_handle=None)._backend.contexts[0]
    assert "conversation" in cold.lower() and "conversation" not in resumed.lower()


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
