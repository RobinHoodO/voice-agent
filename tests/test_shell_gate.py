"""The shell, per surface — and the confirm gate that outlived it.

Robin's ruling (2026-08-10): there is ONE brain and it runs on his Mac. The desk surface
is him sitting at that machine, so its `run_shell` runs what it is told, immediately,
exactly as it always has. The phone surface is the same brain reached from his pocket,
and it has NO `run_shell` at all — not gated, not staged, absent from the schema and
refused by the dispatcher.

What used to be in this file was the third option: a full shell on Thrivbe-1 whose
"destructive" commands staged behind the spoken gate. That needed a classifier to decide
which commands were reads, and three rounds of adversarial review broke it — `env` as an
exec wrapper, `git ls-remote --upload-pack`, `uniq`'s second operand writing a file. The
approach was abandoned rather than patched a fourth time, and `core/destructive.py` went
with it.

The GATE did not go with it. `gmail_send` and `kernel_decide` still stage and still wait
for a plain spoken yes, and every property that machinery had must still hold — so the
second half of this file is those properties, re-anchored on the tools that still use
them:

  * DENY beats AFFIRM in the same utterance ("yes — no, stop");
  * the stage expires (`PENDING_ACTION_TTL_SECONDS`);
  * an unrelated sentence drops it rather than executing it;
  * only ONE action is staged at a time, and the FIRST one wins (the bait-and-switch).
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


async def send_mail(session, to="anna@example.com", call_id="c1"):
    """Stage a `gmail_send` and hand back what the model was told.

    The socket carries more than tool results (a `response.create` follows every one),
    so the frame is picked by shape rather than by position.
    """
    session._ws.sent.clear()
    await session._do_tool({"call_id": call_id, "name": "gmail_send",
                            "arguments": json.dumps({"to": to, "subject": "Notes",
                                                     "body": "hi"})})
    return next(f for f in session._ws.sent if "item" in f)["item"]["output"]


def run(coro_factory):
    async def scenario():
        session, coro = coro_factory()
        session._loop = asyncio.get_running_loop()
        return await coro(session)
    return asyncio.run(scenario())


# ── the desk is unchanged ────────────────────────────────────────────────────
def test_the_mac_shell_still_runs_everything_immediately(monkeypatch):
    """Robin is sitting at this machine. It works like it works now."""
    session = make_session("mac", monkeypatch)

    async def scenario(s):
        out = await call_shell(s, "rm -rf /tmp/whatever")
        assert s._shell.ran == ["rm -rf /tmp/whatever"]
        assert s._pending_action is None
        assert "CONFIRMATION REQUIRED" not in out

    run(lambda: (session, scenario))


def test_the_desk_shell_does_not_classify_what_it_runs(monkeypatch):
    """No allowlist survived, in either direction: the commands that used to STAGE and
    the commands that used to run FREE now take the same path, because nothing looks at
    the text of a command any more."""
    session = make_session("mac", monkeypatch)

    async def scenario(s):
        for command in ("ls /opt", "env rm -rf /opt", "git ls-remote --upload-pack=sh",
                        "uniq /etc/hosts /tmp/written"):
            await call_shell(s, command)
            s._ws.sent.clear()
        assert s._shell.ran == ["ls /opt", "env rm -rf /opt",
                                "git ls-remote --upload-pack=sh",
                                "uniq /etc/hosts /tmp/written"]
        assert s._pending_action is None

    run(lambda: (session, scenario))


# ── the phone has no shell, and that is enforced twice ──────────────────────
def test_the_phone_has_no_run_shell_in_its_schema():
    """The first fence: the model is never told the tool exists."""
    from core.tools import TOOLS

    assert "run_shell" not in {t["name"] for t in capabilities.tools_for("phone", TOOLS)}


def test_a_shell_call_on_the_phone_is_refused_and_nothing_runs(monkeypatch):
    """The second fence, and the one that matters: the schema is a REQUEST to the
    provider, not an enforcement point. A stale session or a hallucinated name still
    arrives at `_do_tool`, and it must not reach the shell."""
    session = make_session("phone", monkeypatch)

    async def scenario(s):
        out = await call_shell(s, "rm -rf /opt/Thrivbe-AI")
        assert s._shell.ran == [], "a phone call reached the Mac's shell"
        assert s._pending_action is None, "it was staged — the phone stages nothing"
        assert "no run_shell tool on this surface" in out

    run(lambda: (session, scenario))


def test_an_unnamed_tool_call_on_the_phone_is_refused_too(monkeypatch):
    """The provider may omit the tool name; core defaults that to run_shell. If the
    refusal were keyed only to a NAMED call, "no name" would be the way around it —
    straight into `_shell_tool`, which no longer has a gate to catch it."""
    session = make_session("phone", monkeypatch)

    async def scenario(s):
        out = await call_shell(s, "rm -rf /opt/Thrivbe-AI", name=None)
        assert s._shell.ran == []
        assert "no run_shell tool on this surface" in out

    run(lambda: (session, scenario))


def test_an_unregistered_surface_has_no_shell_either(monkeypatch):
    """Fail closed: no profile registered anywhere is the strict fallback, not the desk."""
    monkeypatch.setattr(live_session.caps, "_profile", None, raising=False)
    session = make_session(None, monkeypatch)

    async def scenario(s):
        assert not capabilities.has_shell(s.profile_name)
        await call_shell(s, "rm -rf /opt/voice-agent")
        assert s._shell.ran == []

    run(lambda: (session, scenario))


# ── the confirm gate, which stays ───────────────────────────────────────────
def test_a_high_stakes_tool_is_staged_and_does_not_run(monkeypatch):
    session = make_session("mac", monkeypatch)
    sent = []
    monkeypatch.setattr(live_session.services, "gmail_send",
                        lambda args: sent.append(args) or "sent")

    async def scenario(s):
        out = await send_mail(s)
        assert sent == [], "the email went out before Robin said anything"
        assert s._pending_action["tool"] == "gmail_send"
        assert "CONFIRMATION REQUIRED" in out
        # The preview has to be checkable BY EAR: who it goes to, what it says.
        assert "anna@example.com" in out

    run(lambda: (session, scenario))


def test_a_spoken_yes_runs_the_staged_action(monkeypatch):
    session = make_session("mac", monkeypatch)
    sent = []
    monkeypatch.setattr(live_session.services, "gmail_send",
                        lambda args: sent.append(args) or "sent")

    async def scenario(s):
        await send_mail(s)
        await s._resolve_pending_action("yes")
        assert len(sent) == 1
        assert s._pending_action is None
        # The result is fed back so she can say what happened.
        assert any("has executed" in json.dumps(frame) for frame in s._ws.sent)

    run(lambda: (session, scenario))


@pytest.mark.parametrize("utterance", ["no", "yes no", "stop", "nei"])
def test_a_deny_drops_the_staged_action(monkeypatch, utterance):
    """DENY beats AFFIRM inside one utterance — "yes, no wait" must not send anything."""
    session = make_session("mac", monkeypatch)
    sent = []
    monkeypatch.setattr(live_session.services, "gmail_send",
                        lambda args: sent.append(args) or "sent")

    async def scenario(s):
        await send_mail(s)
        await s._resolve_pending_action(utterance)
        assert sent == []
        assert s._pending_action is None

    run(lambda: (session, scenario))


def test_an_unrelated_sentence_drops_the_staged_action(monkeypatch):
    """Silence about the gate is not consent. He moved on; the action dies with the
    subject."""
    session = make_session("mac", monkeypatch)
    sent = []
    monkeypatch.setattr(live_session.services, "gmail_send",
                        lambda args: sent.append(args) or "sent")

    async def scenario(s):
        await send_mail(s)
        await s._resolve_pending_action("what's on my calendar tomorrow")
        assert sent == []
        assert s._pending_action is None

    run(lambda: (session, scenario))


def test_a_stale_stage_expires_instead_of_running(monkeypatch):
    """A "yes" two minutes later is answering something else."""
    session = make_session("mac", monkeypatch)
    sent = []
    monkeypatch.setattr(live_session.services, "gmail_send",
                        lambda args: sent.append(args) or "sent")

    async def scenario(s):
        await send_mail(s)
        s._pending_action["ts"] -= live_session.PENDING_ACTION_TTL_SECONDS + 1
        await s._resolve_pending_action("yes")
        assert sent == []

    run(lambda: (session, scenario))


def test_only_one_action_is_staged_at_a_time_and_the_FIRST_one_wins(monkeypatch):
    """Two staged actions and one spoken "yes" is a coin flip about which one runs.

    This used to assert last-wins (the second stage silently replaced the first). That
    was the bug, not the contract: the preview Robin HEARS is the first one, so
    last-wins means his "yes" executes something he was never read. The slot is
    first-wins — the second attempt is refused, not staged, not run.
    """
    session = make_session("mac", monkeypatch)
    sent = []
    monkeypatch.setattr(live_session.services, "gmail_send",
                        lambda args: sent.append(args) or "sent")

    async def scenario(s):
        await send_mail(s, to="first@example.com")
        s._ws.sent.clear()
        out = await send_mail(s, to="second@example.com", call_id="c2")
        assert "REFUSED" in out
        assert "first@example.com" in out, "the refusal has to name what is still pending"
        assert s._pending_action["args"]["to"] == "first@example.com"
        await s._resolve_pending_action("yes")
        assert [a["to"] for a in sent] == ["first@example.com"]

    run(lambda: (session, scenario))


def test_a_second_stage_is_refused_only_while_the_first_is_live(monkeypatch):
    """…and an EXPIRED stage does not wedge the gate shut for the rest of the TTL."""
    session = make_session("mac", monkeypatch)
    sent = []
    monkeypatch.setattr(live_session.services, "gmail_send",
                        lambda args: sent.append(args) or "sent")

    async def scenario(s):
        await send_mail(s, to="first@example.com")
        s._pending_action["ts"] -= live_session.PENDING_ACTION_TTL_SECONDS + 1
        out = await send_mail(s, to="second@example.com", call_id="c2")
        assert "CONFIRMATION REQUIRED" in out
        assert s._pending_action["args"]["to"] == "second@example.com"
        await s._resolve_pending_action("yes")
        assert [a["to"] for a in sent] == ["second@example.com"], \
            "the dead stage must not resurrect"

    run(lambda: (session, scenario))


def test_the_bait_and_switch_cannot_swap_the_payload_under_a_spoken_yes(monkeypatch):
    """The attack the one-slot supersede used to allow, in full.

    The model stages an innocuous `gmail_send`, SAYS that preview out loud ("about to
    send an email to anna…"), and then — before Robin answers — stages a kernel
    decision. With a last-wins slot his single "yes", spoken about the email he heard,
    executed the decision he never heard. The second stage is now refused, so the only
    thing his yes can execute is the thing he was actually read.
    """
    session = make_session("mac", monkeypatch)
    sent, decided = [], []
    monkeypatch.setattr(live_session.services, "gmail_send",
                        lambda args: sent.append(args) or "sent")
    monkeypatch.setattr(live_session.kernel_tools, "kernel_decide",
                        lambda args: decided.append(args) or "decided")

    async def scenario(s):
        heard = await send_mail(s)
        assert "anna@example.com" in heard           # this is what Robin is read
        s._ws.sent.clear()
        await s._do_tool({"call_id": "c2", "name": "kernel_decide",
                          "arguments": json.dumps({"approvalId": 7,
                                                   "decision": "approve"})})
        refusal = next(f for f in s._ws.sent if "item" in f)["item"]["output"]
        assert "REFUSED" in refusal
        assert s._pending_action["tool"] == "gmail_send", "the slot was swapped"
        await s._resolve_pending_action("yes")
        assert decided == [], "his yes executed a decision he was never read"
        assert len(sent) == 1, "his yes should still send the email he WAS read"

    run(lambda: (session, scenario))


def test_the_gate_is_one_mechanism_rather_than_one_per_tool(monkeypatch):
    """Guarding the shape, not just the behaviour. A private per-tool confirmation path
    would pass every test above and then diverge — different TTL, different affirm
    words, a second slot the first tool could not see."""
    session = make_session("mac", monkeypatch)
    monkeypatch.setattr(live_session.services, "gmail_send", lambda args: "sent")

    async def scenario(s):
        await send_mail(s)
        staged = s._pending_action
        assert set(staged) == {"tool", "args", "ts"}, staged
        assert live_session._pending_confirmation_outcome(staged, "yes") == "confirmed"
        # …and a kernel_decide meets the SAME occupied slot, rather than a second gate
        # it knows nothing about. It is refused, and the email stays pending.
        await s._do_tool({"call_id": "c2", "name": "kernel_decide",
                          "arguments": json.dumps({"approvalId": 7,
                                                   "decision": "approve"})})
        assert s._pending_action["tool"] == "gmail_send"
        assert any("REFUSED" in json.dumps(frame) for frame in s._ws.sent)

    run(lambda: (session, scenario))
