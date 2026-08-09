"""Absent has to mean absent in the DISPATCHER, not only on the wire.

`capabilities.tools_for` removes a surface's excluded tools from the schema, and
`test_capability_profiles.py` proves it. But the schema is a request to the provider,
not an enforcement point. `_do_tool` dispatches on whatever name arrives, so a stale
session, a hallucinated name or a provider quirk used to reach the Mac handler anyway —
and `delegate` reaches `_run_in_shell` DIRECTLY, which means a backgrounded command on
Thrivbe-1 with no `core.destructive` classification and no confirmation gate at all.

Found by a blind review, which noted it did not demonstrate a provider actually emitting
an out-of-schema name over a live socket. That is the point: this is the layer behind
the wire, and a defence you only have on the wire is a defence you have once.
"""
import asyncio
import json

import pytest

from core import capabilities, live_session

SERVER_EXCLUDED = sorted(capabilities.excluded_tools("server"))


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
    session._cfg = {"live": {"shell_timeout": 5}}
    session._ws = Ws()
    return session


def test_there_is_something_to_guard():
    """A profile that stopped excluding anything would make every test below vacuous."""
    assert SERVER_EXCLUDED, "the server profile excludes nothing — the guard is untested"


@pytest.mark.parametrize("name", SERVER_EXCLUDED)
def test_an_excluded_tool_is_refused_at_dispatch_and_never_reaches_a_handler(
        monkeypatch, name):
    session = _server_session(monkeypatch)
    ran = []
    # Every route out of the excluded handlers, wired to a tripwire. `_run_in_shell` is
    # the one that matters — `delegate` calls it directly with a backgrounded command.
    monkeypatch.setattr(session, "_run_in_shell", lambda command: ran.append(command))
    monkeypatch.setattr(live_session, "delegate_task",
                        lambda *a, **k: ran.append("delegate_task"))
    monkeypatch.setattr(live_session, "fleet", lambda *a, **k: ran.append("fleet"))
    monkeypatch.setattr(live_session, "continue_task",
                        lambda *a, **k: ran.append("continue_task"))
    monkeypatch.setattr(live_session, "close_finished_tasks",
                        lambda *a, **k: ran.append("close_finished_tasks"))
    monkeypatch.setattr(live_session, "close_gate", lambda *a, **k: ran.append("close_gate"))
    monkeypatch.setattr(live_session, "_put_text", lambda *a, **k: ran.append("_put_text"))

    async def scenario():
        session._loop = asyncio.get_running_loop()
        await session._do_tool({"call_id": "x1", "name": name,
                                "arguments": json.dumps({"instruction": "wipe it",
                                                         "text": "hi",
                                                         "task_name": "t"})})
        assert ran == [], f"{name} reached a handler on the server surface: {ran}"
        result = json.dumps(session._ws.sent)
        assert "no " + name + " tool on this surface" in result, result
        assert session._pending_action is None

    asyncio.run(scenario())


def test_the_mac_still_dispatches_everything_it_owns(monkeypatch):
    """The guard is a subtraction driven by the profile, not a blocklist of its own —
    on the Mac, where nothing is excluded, nothing may be refused."""
    monkeypatch.setattr(live_session.config, "activity", lambda _message: None)

    class Session(live_session.LiveSession):
        PROFILE = "mac"

    session = Session()
    session._cfg = {"live": {"shell_timeout": 5}}
    session._ws = Ws()
    calls = []
    monkeypatch.setattr(live_session, "_put_text", lambda text, paste=True: calls.append(text) or "copied")

    async def scenario():
        session._loop = asyncio.get_running_loop()
        await session._do_tool({"call_id": "m1", "name": "put_text",
                                "arguments": json.dumps({"text": "hello"})})
        assert calls == ["hello"]

    asyncio.run(scenario())


def test_the_refusal_is_journalled_rather_than_silently_swallowed(monkeypatch, tmp_path):
    """A tool the model should not have had is exactly the event you want in the record
    weeks later — it is either a stale session or a jailbreak attempt."""
    from core import audit

    monkeypatch.setenv("VOICE_AGENT_ACTIONS_LOG", str(tmp_path / "actions.jsonl"))
    session = _server_session(monkeypatch)
    monkeypatch.setattr(session, "_run_in_shell", lambda command: "should not happen")

    async def scenario():
        session._loop = asyncio.get_running_loop()
        await session._do_tool({"call_id": "j1", "name": "delegate",
                                "arguments": json.dumps({"instruction": "rm -rf /"})})

    asyncio.run(scenario())
    events = [(e["event"], e["tool"]) for e in audit.read_all()]
    assert ("refused_not_on_surface", "delegate") in events, events
