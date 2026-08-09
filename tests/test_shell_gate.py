"""The server's shell gate: full `run_shell` on Thrivbe-1, destructive commands staged.

Robin's ruling, and the thing under test: reads run free, writes wait for his voice.
The gate is NOT a second confirmation mechanism — it is the one `gmail_send` and
`kernel_decide` already use (`_pending_action` + `_resolve_pending_action`), so every
property that machinery already has must still hold for a shell command:

  * DENY beats AFFIRM in the same utterance ("yes — no, stop");
  * the stage expires (`PENDING_ACTION_TTL_SECONDS`);
  * an unrelated sentence drops it rather than executing it;
  * only ONE action is staged at a time.

`test_an_unnamed_tool_call_is_gated_too` is the one guarding the hole: the provider can
emit a tool call with no name, and core defaults that to run_shell. A gate on the named
branch alone would be bypassed by an anonymous call.
"""
import asyncio
import json

import pytest

from core import capabilities, live_session


class FakeShell:
    def __init__(self, *a, **k):
        self.ran = []

    def run(self, command, timeout=20):
        self.ran.append(command)
        return f"ran: {command}"

    def close(self):
        pass


class Ws:
    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(json.loads(message))


def make_session(profile, monkeypatch):
    monkeypatch.setattr(live_session.config, "activity", lambda _message: None)

    class Session(live_session.LiveSession):
        PROFILE = profile

    session = Session()
    session._shell = FakeShell()
    session._cfg = {"live": {"shell_timeout": 5}}
    session._ws = Ws()
    return session


async def call_shell(session, command, name="run_shell"):
    event = {"call_id": "c1", "arguments": json.dumps({"command": command})}
    if name is not None:
        event["name"] = name
    await session._do_tool(event)
    return session._ws.sent[0]["item"]["output"]


def run(coro_factory):
    async def scenario():
        session, coro = coro_factory()
        session._loop = asyncio.get_running_loop()
        return await coro(session)
    return asyncio.run(scenario())


# ── the Mac is unchanged ─────────────────────────────────────────────────────
def test_the_mac_shell_still_runs_everything_immediately(monkeypatch):
    """Robin is sitting at this machine. The gate is for the surface he is not at."""
    session = make_session("mac", monkeypatch)

    async def scenario(s):
        out = await call_shell(s, "rm -rf /tmp/whatever")
        assert s._shell.ran == ["rm -rf /tmp/whatever"]
        assert s._pending_action is None
        assert "CONFIRMATION REQUIRED" not in out

    run(lambda: (session, scenario))


# ── the server: reads free, writes staged ───────────────────────────────────
def test_a_read_runs_free_on_the_server(monkeypatch):
    session = make_session("server", monkeypatch)

    async def scenario(s):
        out = await call_shell(s, "systemctl status voice-agent")
        assert s._shell.ran == ["systemctl status voice-agent"]
        assert s._pending_action is None
        assert "CONFIRMATION REQUIRED" not in out

    run(lambda: (session, scenario))


def test_a_destructive_command_is_staged_and_does_not_run(monkeypatch):
    session = make_session("server", monkeypatch)

    async def scenario(s):
        out = await call_shell(s, "systemctl restart voice-agent")
        assert s._shell.ran == [], "the command ran before Robin said anything"
        assert s._pending_action["tool"] == "run_shell"
        assert s._pending_action["args"]["command"] == "systemctl restart voice-agent"
        assert "CONFIRMATION REQUIRED" in out
        # The preview has to be checkable BY EAR: the machine, the effect, the command.
        # It is a spoken sentence, so it also has to survive being read out loud —
        # reasons come in two grammatical shapes and both end up in this one line.
        assert "Thrivbe-1" in out
        assert "systemctl restart is not a read" in out
        assert out.rstrip().endswith("the command is: systemctl restart voice-agent. "
                                     "It has NOT run. Say what it will do and ask the "
                                     "user to confirm out loud.")

    run(lambda: (session, scenario))


def test_a_spoken_yes_runs_the_staged_command(monkeypatch):
    session = make_session("server", monkeypatch)

    async def scenario(s):
        await call_shell(s, "rm -rf /opt/voice-agent/tmp")
        assert s._shell.ran == []
        await s._resolve_pending_action("yes")
        assert s._shell.ran == ["rm -rf /opt/voice-agent/tmp"]
        assert s._pending_action is None
        # The result is fed back so she can say what happened.
        assert any("has executed" in json.dumps(frame) for frame in s._ws.sent)

    run(lambda: (session, scenario))


@pytest.mark.parametrize("utterance", ["no", "yes no", "stop", "nei"])
def test_a_deny_drops_the_staged_command(monkeypatch, utterance):
    """DENY beats AFFIRM inside one utterance — "yes, no wait" must not delete anything."""
    session = make_session("server", monkeypatch)

    async def scenario(s):
        await call_shell(s, "rm -rf /opt/voice-agent")
        await s._resolve_pending_action(utterance)
        assert s._shell.ran == []
        assert s._pending_action is None

    run(lambda: (session, scenario))


def test_an_unrelated_sentence_drops_the_staged_command(monkeypatch):
    """Silence about the gate is not consent. He moved on; the command dies with the
    subject."""
    session = make_session("server", monkeypatch)

    async def scenario(s):
        await call_shell(s, "rm -rf /opt/voice-agent")
        await s._resolve_pending_action("what's on my calendar tomorrow")
        assert s._shell.ran == []
        assert s._pending_action is None

    run(lambda: (session, scenario))


def test_a_stale_stage_expires_instead_of_running(monkeypatch):
    """A "yes" two minutes later is answering something else."""
    session = make_session("server", monkeypatch)

    async def scenario(s):
        await call_shell(s, "rm -rf /opt/voice-agent")
        s._pending_action["ts"] -= live_session.PENDING_ACTION_TTL_SECONDS + 1
        await s._resolve_pending_action("yes")
        assert s._shell.ran == []

    run(lambda: (session, scenario))


def test_only_one_command_is_staged_at_a_time_and_the_FIRST_one_wins(monkeypatch):
    """Two staged actions and one spoken "yes" is a coin flip about which one runs.

    This test used to assert last-wins (the second stage silently replaced the first).
    That was the bug, not the contract: the preview Robin HEARS is the first one, so
    last-wins means his "yes" executes a command he was never read. The slot is now
    first-wins — the second attempt is refused, not staged, not run.
    """
    session = make_session("server", monkeypatch)

    async def scenario(s):
        await call_shell(s, "rm -rf /opt/a")
        s._ws.sent.clear()
        out = await call_shell(s, "rm -rf /opt/b")
        assert "REFUSED" in out
        assert "rm -rf /opt/a" in out, "the refusal has to name what is still pending"
        assert s._pending_action["args"]["command"] == "rm -rf /opt/a"
        await s._resolve_pending_action("yes")
        assert s._shell.ran == ["rm -rf /opt/a"]

    run(lambda: (session, scenario))


def test_a_second_stage_is_refused_only_while_the_first_is_live(monkeypatch):
    """…and an EXPIRED stage does not wedge the gate shut for the rest of the TTL."""
    session = make_session("server", monkeypatch)

    async def scenario(s):
        await call_shell(s, "rm -rf /opt/a")
        s._pending_action["ts"] -= live_session.PENDING_ACTION_TTL_SECONDS + 1
        out = await call_shell(s, "rm -rf /opt/b")
        assert "CONFIRMATION REQUIRED" in out
        assert s._pending_action["args"]["command"] == "rm -rf /opt/b"
        await s._resolve_pending_action("yes")
        assert s._shell.ran == ["rm -rf /opt/b"], "the dead stage must not resurrect"

    run(lambda: (session, scenario))


def test_the_bait_and_switch_cannot_swap_the_payload_under_a_spoken_yes(monkeypatch):
    """The attack the one-slot supersede used to allow, in full.

    The model stages an innocuous `gmail_send`, SAYS that preview out loud ("about to
    send an email to anna…"), and then — before Robin answers — stages an `rm -rf`. With
    a last-wins slot his single "yes", spoken about the email he heard, executed the
    delete he never heard. The second stage is now refused, so the only thing his yes
    can execute is the thing he was actually read.
    """
    session = make_session("server", monkeypatch)
    sent = []
    monkeypatch.setattr(live_session.services, "gmail_send",
                        lambda args: sent.append(args) or "sent")

    async def scenario(s):
        await s._do_tool({"call_id": "c1", "name": "gmail_send",
                          "arguments": json.dumps({"to": "anna@example.com",
                                                   "subject": "Notes", "body": "hi"})})
        heard = s._ws.sent[0]["item"]["output"]
        assert "anna@example.com" in heard           # this is what Robin is read
        s._ws.sent.clear()                           # …and `call_shell` reads frame 0
        refusal = await call_shell(s, "rm -rf /opt/Thrivbe-AI")
        assert "REFUSED" in refusal
        assert s._pending_action["tool"] == "gmail_send", "the slot was swapped"
        await s._resolve_pending_action("yes")
        assert s._shell.ran == [], "his yes executed a command he was never read"
        assert len(sent) == 1, "his yes should still send the email he WAS read"

    run(lambda: (session, scenario))


def test_an_unnamed_tool_call_is_gated_too(monkeypatch):
    """The provider may omit the tool name; core defaults that to run_shell. If only the
    named branch were gated, "no name" would be the way around the gate."""
    session = make_session("server", monkeypatch)

    async def scenario(s):
        out = await call_shell(s, "rm -rf /opt/voice-agent", name=None)
        assert s._shell.ran == []
        assert "CONFIRMATION REQUIRED" in out

    run(lambda: (session, scenario))


def test_an_unregistered_surface_stages_too(monkeypatch):
    """Fail closed: no profile registered anywhere is the strict fallback, not the Mac."""
    monkeypatch.setattr(live_session.caps, "_profile", None, raising=False)
    session = make_session(None, monkeypatch)

    async def scenario(s):
        assert capabilities.shell_gate(s.profile_name) == \
            capabilities.SHELL_STAGE_DESTRUCTIVE
        await call_shell(s, "rm -rf /opt/voice-agent")
        assert s._shell.ran == []

    run(lambda: (session, scenario))


def test_the_gate_reuses_the_high_stakes_machinery_rather_than_a_second_one(monkeypatch):
    """Guarding the shape, not just the behaviour. A private shell-only confirmation
    path would pass every test above and then diverge — different TTL, different
    affirm words, a second slot a `gmail_send` could not see."""
    session = make_session("server", monkeypatch)

    async def scenario(s):
        await call_shell(s, "rm -rf /opt/voice-agent")
        staged = s._pending_action
        assert set(staged) == {"tool", "args", "ts"}, staged
        assert live_session._pending_confirmation_outcome(staged, "yes") == "confirmed"
        # …and a gmail_send meets the SAME occupied slot, rather than a second gate it
        # knows nothing about. It is refused, and the shell command stays pending.
        await s._do_tool({"call_id": "c2", "name": "gmail_send",
                          "arguments": json.dumps({"to": "a@b.c", "subject": "s",
                                                   "body": "b"})})
        assert s._pending_action["tool"] == "run_shell"
        assert any("REFUSED" in json.dumps(frame) for frame in s._ws.sent)

    run(lambda: (session, scenario))
