"""The auth boundary and session scoping on the phone surface.

voice-bridge's pattern, so these tests are the same shape as the guarantees it has
been holding on Robin's phone: a shared token, a per-tab UUID, and state that cannot
leak between tabs. The last one is the sharp edge — a spoken "yes" confirms an
irreversible action, and there is more than one tab.
"""
import asyncio
import json
import logging
import uuid

import httpx
import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from server_harness import Client, await_for

from conftest import VOICE_TEST_TOKEN as TOKEN


def _run(coro):
    return asyncio.run(coro)


async def _closed_with(url) -> int:
    """Connect and return the close code the server used to reject us."""
    try:
        ws = await connect(url, max_size=None, open_timeout=10)
    except Exception as e:                     # rejected during the handshake
        return getattr(e, "status_code", -1)
    try:
        await asyncio.wait_for(ws.recv(), timeout=10)
    except ConnectionClosed as e:
        return e.rcvd.code if e.rcvd else -1
    finally:
        await ws.close()
    return 0


@pytest.mark.parametrize("token,session", [
    ("wrong-token", "uuid"),          # bad shared secret
    ("", "uuid"),                     # no token at all
    (None, "missing"),                # no session key
    (None, "not-a-uuid"),             # session key that isn't a UUID
])
def test_live_rejects_bad_credentials(voice_server, token, session):
    server, _sessions = voice_server
    tok = TOKEN if token is None else token
    if session == "uuid":
        key = str(uuid.uuid4())
    elif session == "missing":
        key = ""
    else:
        key = session
    url = f"ws://127.0.0.1:{server.port}/live?token={tok}&session={key}"
    assert _run(_closed_with(url)) == 1008


def test_health_requires_the_token(voice_server):
    server, _sessions = voice_server
    assert httpx.get(server.http + "/health", timeout=10).status_code == 401
    assert httpx.get(server.http + "/health",
                     headers={"X-Voice-Token": "nope"}, timeout=10).status_code == 401
    ok = httpx.get(server.http + "/health",
                   headers={"X-Voice-Token": TOKEN}, timeout=10)
    assert ok.status_code == 200
    assert "sessions" in ok.json()


def test_uvicorn_never_logs_the_token():
    """The token rides in the WebSocket URL because browsers cannot set headers on a
    WS handshake, so uvicorn would write it to journald. Both loggers are filtered —
    the "accepted" line comes from uvicorn.error, not uvicorn.access."""
    from server.app import _RedactTokenFilter

    f = _RedactTokenFilter()
    rec = logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1,
                            '%s - "WebSocket %s" [accepted]',
                            ("1.2.3.4", "/live?token=s3cr3t&session=abc"), None)
    assert f.filter(rec)
    rendered = rec.getMessage()
    assert "s3cr3t" not in rendered
    assert "token=***" in rendered

    plain = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
                              "GET /live?token=s3cr3t HTTP/1.1", None, None)
    assert f.filter(plain)
    assert "s3cr3t" not in plain.getMessage()


def test_two_tabs_never_cross_confirm(voice_server, monkeypatch):
    """Two tabs, one staged irreversible action. Saying "yes" in the wrong tab must do
    nothing — the staged action lives on that tab's own LiveSession, and confirmation
    is resolved against that object, not a server-wide slot."""
    from core import live_session

    server, sessions = voice_server
    executed: list[dict] = []
    monkeypatch.setitem(live_session.HIGH_STAKES_EXECUTORS, "gmail_send",
                        lambda args: executed.append(args) or "sent")

    async def scenario():
        key_a, key_b = str(uuid.uuid4()), str(uuid.uuid4())
        ws_a = await connect(server.ws_url(TOKEN, key_a), max_size=None)
        ws_b = await connect(server.ws_url(TOKEN, key_b), max_size=None)
        ca, cb = Client(ws_a), Client(ws_b)
        await ca.__aenter__()
        await cb.__aenter__()
        try:
            await ca.expect("audio")
            await cb.expect("audio")
            assert len(sessions) == 2
            sess_a, sess_b = sessions[0], sessions[1]
            assert sess_a is not sess_b

            # Tab A's model asks to send an email — gated, so it is only STAGED.
            sess_a.fake.push(sess_a._loop, {
                "kind": "tool_call", "call_id": "call-a", "name": "gmail_send",
                "args": json.dumps({"to": "robin@meta.thrivbe.com", "subject": "tab A"}),
            })
            await await_for(lambda: sess_a._pending_action, what="A's staged action")
            assert sess_b._pending_action is None

            # Tab B says yes. It is a real, well-formed confirmation — in the wrong tab.
            sess_b.fake.push(sess_b._loop, {"kind": "user_transcript", "text": "yes"})
            await await_for(lambda: cb.of("turn"), what="B's transcript to land")
            await asyncio.sleep(0.4)
            assert executed == [], "a yes in tab B confirmed tab A's action"
            assert sess_a._pending_action is not None, "A's action was consumed by B"

            # …and the same words in tab A do confirm it, exactly once.
            sess_a.fake.push(sess_a._loop, {"kind": "user_transcript", "text": "yes"})
            await await_for(lambda: executed, what="A's confirmation to execute")
            assert len(executed) == 1
            assert executed[0]["subject"] == "tab A"
            assert sess_a._pending_action is None
        finally:
            await ca.__aexit__()
            await cb.__aexit__()
            await ws_a.close()
            await ws_b.close()

    _run(scenario())


def test_a_reconnecting_tab_replaces_its_own_session(voice_server):
    """Same UUID twice = the same tab after a dropped mobile network. The old session
    is retired rather than left running beside the new one — two sessions on one key
    would mean two confirmation slots for one conversation."""
    server, sessions = voice_server

    async def scenario():
        key = str(uuid.uuid4())
        ws1 = await connect(server.ws_url(TOKEN, key), max_size=None)
        c1 = Client(ws1)
        await c1.__aenter__()
        await c1.expect("audio")
        first = sessions[-1]

        ws2 = await connect(server.ws_url(TOKEN, key), max_size=None)
        c2 = Client(ws2)
        await c2.__aenter__()
        await c2.expect("audio")
        second = sessions[-1]
        assert second is not first

        await await_for(lambda: not first._running, what="the old session to stop")
        from server.app import SESSIONS
        assert len(SESSIONS) == 1
        await c1.__aexit__()
        await c2.__aexit__()
        await ws1.close()
        await ws2.close()

    _run(scenario())
