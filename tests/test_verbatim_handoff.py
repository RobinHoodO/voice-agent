"""Fidelity of the verbatim block attached to every delegated handoff.

The load-bearing case is the transcript RACE: Whisper transcription is a side-channel
running in parallel with the model's response, and measured against real session logs
it lands AFTER the tool call ~15% of the time. Without the wait, the handoff would
carry the PREVIOUS turn's words under a "Robin's own words (verbatim)" label — a
confidently mislabelled quote, worse than no quote at all.
"""
import asyncio
import json

from core import live_session


class _FakeWS:
    """_do_tool reports the result back over the socket; tests only care about args."""
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)


def _session():
    # Own the loop: other suites in this repo close theirs, so get_event_loop() is
    # unreliable once the whole suite runs in one process.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    s = live_session.LiveSession()
    s._loop = loop
    s._ws = _FakeWS()
    return s


def _dispatch(session, name, args, late_turn=None, delay=0.3):
    """Run one tool dispatch, optionally landing a transcript mid-flight."""
    captured = {}

    async def fake_exec(_none, fn, *a):
        captured["args"] = a
        return "ok"

    session._loop.run_in_executor = fake_exec
    session.on_state = lambda _s: None

    async def drive():
        if late_turn is not None:
            async def land():
                await asyncio.sleep(delay)
                session._turns.append(late_turn)
            asyncio.ensure_future(land())
        await session._do_tool({"call_id": "c1", "name": name,
                                "arguments": json.dumps(args)})

    session._loop.run_until_complete(drive())
    return captured.get("args", ())


def test_late_transcript_is_waited_for_not_mislabelled():
    """The race case: tool call fires before Whisper's transcript lands."""
    s = _session()
    s._turns = ["you: close the old pane", "agent: done"]
    s._delegated_upto = 2          # previous turn already consumed

    sent = _dispatch(s, "delegate", {"instruction": "review the lemclone failures"},
                     late_turn="you: actually review the LemClone revenue loop failures")

    instruction = sent[0]
    assert "actually review the LemClone revenue loop failures" in instruction, \
        "the late-arriving transcript must be waited for and included"
    assert "close the old pane" not in instruction, \
        "must never present a PREVIOUS turn's words as this request's verbatim quote"


def test_absent_transcript_yields_no_verbatim_block():
    """Timeout must omit the block entirely rather than quote something stale."""
    s = _session()
    s._turns = ["you: close the old pane", "agent: done"]
    s._delegated_upto = 2

    async def instant(_self, timeout=2.0):
        return                      # simulate the wait expiring
    s._await_transcript = instant.__get__(s)

    sent = _dispatch(s, "delegate", {"instruction": "do the thing"})
    instruction = sent[0]
    assert "Robin's own words" not in instruction
    assert "close the old pane" not in instruction
    assert "do the thing" in instruction


def test_os_delegate_is_wrapped_despite_the_high_stakes_gate():
    """os_delegate sits in the fail-closed high-stakes set; wrapping happens before
    the elif chain so it can't be preempted into dead code."""
    s = _session()
    s._high_stakes = {"os_delegate"}          # gated => staged, not executed
    s._turns = ["you: chase the unpaid invoice"]
    s._delegated_upto = 0

    s._loop.run_until_complete(
        s._do_tool({"call_id": "c1", "name": "os_delegate",
                    "arguments": json.dumps({"instruction": "chase invoice"})}))

    staged = s._pending_action["args"]["instruction"]
    assert "chase the unpaid invoice" in staged, \
        "the gated path must still carry Robin's verbatim words"


def test_continue_task_survives_the_single_line_flattening():
    """continue_task collapses whitespace before sending into a live lane, so the
    block must stay readable — and must not carry the memory pointer into a lane
    that already has full context."""
    s = _session()
    s._turns = ["you: also handle the timeout case"]
    s._delegated_upto = 0

    sent = _dispatch(s, "continue_task", {"task_name": "routing-fix",
                                          "feedback": "handle timeouts"})
    feedback = sent[0]["feedback"]
    assert "memory.py recall" not in feedback, "no memory pointer into a mid-task lane"
    assert "also handle the timeout case" in " ".join(feedback.split())


def test_cursor_advances_so_words_are_not_repeated():
    s = _session()
    s._turns = ["you: first request"]
    s._delegated_upto = 0

    first = _dispatch(s, "delegate", {"instruction": "a"})[0]
    assert "first request" in first

    s._turns.append("you: second request")
    second = _dispatch(s, "delegate", {"instruction": "b"})[0]
    assert "second request" in second
    assert "first request" not in second, "already-handed-off words must not repeat"
