"""What counts as a spoken YES — the table, and the same table driven end-to-end.

The gate used to ask "are there at most four words, and does an affirm word appear
anywhere in them?". Containment is not consent. A blind review drove the real session
with a staged `rm -rf /opt/voice-agent` and got it to execute on: "did you approve
that?", "why would I confirm?", "confirm what exactly", "yes but wait", "yes, hold on",
"approved yesterday", "proceed?" and "did it go ahead". Not one of those is a person
telling a machine to delete a directory.

So there are two layers of test here, deliberately:

  * `test_the_affirm_table` — the predicate, cheap and exhaustive, both bars;
  * `test_the_staged_rm_survives_every_ambiguous_utterance` — the same strings against
    the REAL `_shell_tool` + `_resolve_pending_action`, asserting the FakeShell never
    ran. A predicate test alone would pass even if the gate stopped consulting it.

`ASYMMETRY` is the third piece: `capabilities.CONFIRM_STRICTNESS` says an irreversible
shell command needs a plainer yes than a recoverable email, and that difference has to
be visible in a test or it will be optimised away by someone tidying the vocabulary.
"""
import asyncio
import json

import pytest

from core import capabilities, live_session

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
class FakeShell:
    def __init__(self):
        self.ran = []

    def run(self, command, timeout=20):
        self.ran.append(command)
        return f"ran: {command}"


class Ws:
    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(json.loads(message))


def _server_session(monkeypatch):
    monkeypatch.setattr(live_session.config, "activity", lambda _message: None)

    class Session(live_session.LiveSession):
        PROFILE = "server"

    session = Session()
    session._shell = FakeShell()
    session._cfg = {"live": {"shell_timeout": 5}}
    session._ws = Ws()
    return session


AMBIGUOUS = [row[0] for row in AFFIRM_TABLE if not row[2] and row[0].strip()]
UNAMBIGUOUS = [row[0] for row in AFFIRM_TABLE if row[2]]


@pytest.mark.parametrize("utterance", AMBIGUOUS)
def test_the_staged_rm_survives_every_ambiguous_utterance(monkeypatch, utterance):
    """Stage the delete fresh, say the thing, assert nothing was deleted. This is the
    blind review's own harness, kept as a regression."""
    session = _server_session(monkeypatch)

    async def scenario():
        session._loop = asyncio.get_running_loop()
        out = await session._shell_tool({"command": "rm -rf /opt/voice-agent"})
        assert "CONFIRMATION REQUIRED" in out
        await session._resolve_pending_action(utterance)
        assert session._shell.ran == [], \
            f"{utterance!r} executed a staged rm -rf"
        assert session._pending_action is None, \
            f"{utterance!r} left the action staged for the NEXT utterance to catch"

    asyncio.run(scenario())


@pytest.mark.parametrize("utterance", UNAMBIGUOUS)
def test_a_real_yes_still_runs_the_staged_command(monkeypatch, utterance):
    """The other direction. A gate nobody can pass is a gate Robin routes around."""
    session = _server_session(monkeypatch)

    async def scenario():
        session._loop = asyncio.get_running_loop()
        await session._shell_tool({"command": "rm -rf /opt/voice-agent/tmp"})
        await session._resolve_pending_action(utterance)
        assert session._shell.ran == ["rm -rf /opt/voice-agent/tmp"], \
            f"{utterance!r} was meant to be a yes"

    asyncio.run(scenario())


def test_a_weak_yes_confirms_an_email_but_not_a_delete(monkeypatch):
    """The asymmetry, end to end and in one place: the SAME word, two staged tools,
    two answers."""
    session = _server_session(monkeypatch)
    sent = []
    monkeypatch.setattr(live_session.services, "gmail_send",
                        lambda args: sent.append(args) or "sent")

    async def scenario():
        session._loop = asyncio.get_running_loop()
        await session._shell_tool({"command": "rm -rf /opt/voice-agent"})
        await session._resolve_pending_action("approved")
        assert session._shell.ran == [], "'approved' deleted a directory"

        await session._do_tool({"call_id": "c9", "name": "gmail_send",
                                "arguments": json.dumps({"to": "a@b.c", "subject": "s",
                                                         "body": "b"})})
        await session._resolve_pending_action("approved")
        assert len(sent) == 1, "'approved' should still confirm a recoverable email"

    asyncio.run(scenario())
