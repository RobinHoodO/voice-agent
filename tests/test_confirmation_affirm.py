"""What counts as a spoken YES — the table, and the same table driven end-to-end.

The gate used to ask "are there at most four words, and does an affirm word appear
anywhere in them?". Containment is not consent. A blind review drove the real session
with a staged `rm -rf /opt/voice-agent` and got it to execute on: "did you approve
that?", "why would I confirm?", "confirm what exactly", "yes but wait", "yes, hold on",
"approved yesterday", "proceed?" and "did it go ahead". Not one of those is a person
telling a machine to delete a directory.

So there are two layers of test here, deliberately:

  * `test_the_affirm_table` — the predicate, cheap and exhaustive, both bars;
  * `test_the_staged_rm_survives_every_ambiguous_utterance` — the same strings against a
    REAL `confirm_gate.PendingSlot` holding a real `rm -rf`, and
    `test_the_staged_email_survives_every_ambiguous_utterance` — the same, through a
    real `LiveSession`. A predicate test alone would pass even if the gate stopped
    consulting it.

The asymmetry is the third piece: `capabilities.CONFIRM_STRICTNESS` says an irreversible
shell command needs a plainer yes than a recoverable email, and that difference has to
be visible in a test or it will be optimised away by someone tidying the vocabulary.

Why the shell half no longer drives a `LiveSession`: no live conversation stages a shell
command any more. The desk surface runs what it is told (Robin is sitting there) and the
phone surface has no `run_shell` at all, so the surface that still stages one is
`mac/reverse_channel.py` — and it stages into this same `PendingSlot`. The vocabulary is
one implementation; which object holds it is not what these strings are about.
"""
import asyncio
import json

import pytest

from core import capabilities, confirm_gate, live_session

NORMAL = capabilities.AFFIRM_NORMAL
STRICT = capabilities.AFFIRM_STRICT


# ── the predicate ────────────────────────────────────────────────────────────
# Every string from the blind review's finding-1 proof, plus the ones it confirmed were
# already dropping (the no-regression half), plus the affirmations that must keep
# working or Robin cannot approve anything by voice.
AFFIRM_TABLE = [
    # (utterance, is a yes at the NORMAL bar?, is a yes at the STRICT bar?)
    # --- finding 1: these EXECUTED a staged rm -rf. All must be no, at both bars. ---
    ("did you approve that?",       False, False),
    ("why would I confirm?",        False, False),
    ("confirm what exactly",        False, False),
    ("yes but wait",                False, False),
    ("yes, hold on",                False, False),
    ("approved yesterday",          False, False),
    ("proceed?",                    False, False),
    ("did it go ahead",             False, False),
    # --- no regressions: already dropped, must keep dropping ---
    ("I said no",                   False, False),
    ("don't do it",                 False, False),
    ("does it delete anything",     False, False),
    ("what does that command do",   False, False),
    # --- more of the same shapes, since the fix is a class of bug, not eight strings --
    ("yes hang on",                 False, False),
    ("yes, actually wait",          False, False),
    ("should I confirm",            False, False),
    ("confirm it later",            False, False),
    ("go ahead and explain",        False, False),
    ("ja, men vent",                False, False),
    ("did you send it",             False, False),
    ("who approved it",             False, False),
    ("yes if you must",             False, False),
    ("send it tomorrow",            False, False),
    ("approve the invoice first",   False, False),
    # --- "please X" is Robin ASKING for X, not consenting to it. "please" used to be
    # stripped as courtesy from either end, which left "confirm" — a complete
    # affirmation — and cleared the normal bar. It is courtesy only at the END.
    ("please confirm",              False, False),
    ("please approve",              False, False),
    ("please confirm it",           False, False),
    ("please go ahead",             False, False),
    ("please do it",                False, False),
    ("vennligst bekreft",           False, False),
    ("",                            False, False),
    ("   ",                         False, False),
    # --- real affirmations: these must keep working at BOTH bars ---
    ("yes",                         True,  True),
    ("Yes.",                        True,  True),
    ("yeah",                        True,  True),
    ("yep",                         True,  True),
    ("go ahead",                    True,  True),
    ("go",                          True,  True),
    ("do it",                       True,  True),
    ("yes do it",                   True,  True),
    ("yes please",                  True,  True),
    ("ja, kjør",                    True,  True),
    ("kjør det",                    True,  True),
    ("ok, do it now",               True,  True),
    # --- the asymmetry: good enough for an email, not for an irreversible command ---
    ("confirm",                     True,  False),
    ("confirmed",                   True,  False),
    ("approve it",                  True,  False),
    ("approved",                    True,  False),
    ("proceed",                     True,  False),
    ("send it",                     True,  False),
    ("gjør det",                    True,  False),
]


@pytest.mark.parametrize("utterance,normal,strict", AFFIRM_TABLE)
def test_the_affirm_table(utterance, normal, strict):
    assert live_session._is_short_affirm(utterance, NORMAL) is normal, \
        f"NORMAL bar disagrees about {utterance!r}"
    assert live_session._is_short_affirm(utterance, STRICT) is strict, \
        f"STRICT bar disagrees about {utterance!r}"


def test_the_default_bar_is_the_one_the_old_signature_used():
    """`_is_short_affirm(text)` with no strictness is still the normal bar, so every
    existing caller keeps its meaning."""
    assert live_session._is_short_affirm("go ahead") is True
    assert live_session._is_short_affirm("yes please record the decision now") is False


def test_run_shell_is_the_strict_one_and_that_is_data():
    """The asymmetry lives next to PROFILES, not as a constant in the gate."""
    assert capabilities.confirm_strictness("run_shell") == capabilities.AFFIRM_STRICT
    assert capabilities.confirm_strictness("gmail_send") == capabilities.AFFIRM_NORMAL
    assert capabilities.confirm_strictness(None) == capabilities.DEFAULT_CONFIRM_STRICTNESS


# ── the same strings, against the real gate ──────────────────────────────────
# Two bars, two harnesses, because as of 2026-08-10 they live in different places.
#
#   NORMAL — `gmail_send` in a real `LiveSession`, through `_do_tool` and
#            `_resolve_pending_action`. Unchanged.
#   STRICT — `run_shell`, through a real `confirm_gate.PendingSlot`. No live
#            conversation stages a shell command any more: the desk surface runs what it
#            is told and the phone surface has no shell at all, so the surface that
#            still stages one is `mac/reverse_channel.py`, which holds THIS object.
#            Driving the slot directly is the honest way to say "the strict bar is still
#            wired to the vocabulary", and `tests/test_reverse_channel.py` carries the
#            same assertion over real HTTP.
#
# Either way the point is the same one the predicate tests cannot make: a table test
# would still pass if the gate stopped consulting the table.

class Ws:
    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(json.loads(message))


def _session(monkeypatch):
    monkeypatch.setattr(live_session.config, "activity", lambda _message: None)

    class Session(live_session.LiveSession):
        PROFILE = "mac"

    session = Session()
    session._cfg = {"live": {"shell_timeout": 5}}
    session._ws = Ws()
    return session


def _staged_shell_slot():
    """A real gate holding a real `rm -rf`, exactly as the reverse channel stages one."""
    slot = confirm_gate.PendingSlot()
    slot.stage("run_shell", {"command": "rm -rf /opt/voice-agent"})
    return slot


AMBIGUOUS = [row[0] for row in AFFIRM_TABLE if not row[2] and row[0].strip()]
UNAMBIGUOUS = [row[0] for row in AFFIRM_TABLE if row[2]]
AMBIGUOUS_AT_THE_NORMAL_BAR = [row[0] for row in AFFIRM_TABLE
                               if not row[1] and row[0].strip()]


@pytest.mark.parametrize("utterance", AMBIGUOUS)
def test_the_staged_rm_survives_every_ambiguous_utterance(utterance):
    """Stage the delete fresh, say the thing, assert it was not confirmed. This is the
    blind review's own harness, kept as a regression."""
    outcome, pending = _staged_shell_slot().resolve(utterance)
    assert pending["args"]["command"] == "rm -rf /opt/voice-agent"
    assert outcome != "confirmed", f"{utterance!r} executed a staged rm -rf"


@pytest.mark.parametrize("utterance", UNAMBIGUOUS)
def test_a_real_yes_still_confirms_the_staged_command(utterance):
    """The other direction. A gate nobody can pass is a gate Robin routes around."""
    outcome, _pending = _staged_shell_slot().resolve(utterance)
    assert outcome == "confirmed", f"{utterance!r} was meant to be a yes"


@pytest.mark.parametrize("utterance", AMBIGUOUS_AT_THE_NORMAL_BAR)
def test_the_staged_email_survives_every_ambiguous_utterance(monkeypatch, utterance):
    """The normal bar, end to end through a live session: staged, spoken at, not sent."""
    session = _session(monkeypatch)
    sent = []
    monkeypatch.setattr(live_session.services, "gmail_send",
                        lambda args: sent.append(args) or "sent")

    async def scenario():
        session._loop = asyncio.get_running_loop()
        await session._do_tool({"call_id": "c1", "name": "gmail_send",
                                "arguments": json.dumps({"to": "a@b.c", "subject": "s",
                                                         "body": "b"})})
        assert session._pending_action["tool"] == "gmail_send"
        await session._resolve_pending_action(utterance)
        assert sent == [], f"{utterance!r} sent a staged email"
        assert session._pending_action is None, \
            f"{utterance!r} left the action staged for the NEXT utterance to catch"

    asyncio.run(scenario())


def test_a_weak_yes_confirms_an_email_but_not_a_delete(monkeypatch):
    """The asymmetry, end to end and in one place: the SAME word, two staged tools,
    two answers."""
    session = _session(monkeypatch)
    sent = []
    monkeypatch.setattr(live_session.services, "gmail_send",
                        lambda args: sent.append(args) or "sent")

    outcome, _pending = _staged_shell_slot().resolve("approved")
    assert outcome != "confirmed", "'approved' confirmed a delete"

    async def scenario():
        session._loop = asyncio.get_running_loop()
        await session._do_tool({"call_id": "c9", "name": "gmail_send",
                                "arguments": json.dumps({"to": "a@b.c", "subject": "s",
                                                         "body": "b"})})
        await session._resolve_pending_action("approved")
        assert len(sent) == 1, "'approved' should still confirm a recoverable email"

    asyncio.run(scenario())
