"""What happens when the phone connects while Robin is talking at his desk.

One brain, so this is a real case rather than a hypothetical, and the answer is written
down in `core/floor.py`: a CONNECTION never ends a conversation, a deliberate human act
may. These tests drive the whole surface — a real uvicorn, a real WebSocket, a real
`LiveSession` — because the property is about what does and does not get started, and
that is only observable end to end.

The desk is stood in for by a direct `floor.claim(DESK, …)`, which is exactly what
`mac/agent.py::toggle_live` does; the menubar itself cannot be imported here (rumps
needs a running NSApplication).
"""
import asyncio
import uuid

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from server_harness import Client, await_for

from conftest import VOICE_TEST_TOKEN as TOKEN


def _run(coro):
    return asyncio.run(coro)


class DeskSession:
    """A stand-in for the menubar's LiveSession: it records being asked to stop."""

    def __init__(self):
        self.stopped = 0

    def stop(self):
        self.stopped += 1


def _desk_takes_the_floor():
    """Claim the floor as the desk, wired the way mac/agent.py wires it."""
    from core import floor

    session = DeskSession()
    floor.claim(floor.DESK, "desk:menubar", session=session, on_evict=session.stop)
    return session


async def _drain(ws, client):
    await client.__aexit__()
    await ws.close()


# ── 1. a phone that just connects is refused, and the desk is untouched ──────
def test_a_phone_connecting_mid_desk_conversation_is_refused(voice_server):
    """The whole point. Robin is mid-sentence at his desk; his phone, in his pocket,
    reconnects the PWA. Nothing of his may be stopped, and nothing expensive may be
    started — no LiveSession, and above all no realtime socket."""
    from core import floor
    from server.app import FLOOR_BUSY_CLOSE, SESSIONS

    server, sessions = voice_server
    desk = _desk_takes_the_floor()

    async def scenario():
        ws = await connect(server.ws_url(TOKEN, str(uuid.uuid4())), max_size=None)
        client = Client(ws)
        await client.__aenter__()
        busy = await client.expect("busy")
        assert busy["holder"] == "desk"
        assert "desk" in busy["text"]
        try:
            await asyncio.wait_for(ws.recv(), timeout=5)
        except ConnectionClosed as e:
            assert e.rcvd.code == FLOOR_BUSY_CLOSE
        await _drain(ws, client)

    _run(scenario())

    assert desk.stopped == 0, "the desk conversation was ended by a phone CONNECTING"
    assert floor.holder().surface == floor.DESK, "the phone took the floor without asking"
    assert sessions == [], "a refused phone still built a LiveSession"
    assert SESSIONS == {}


# ── 2. …and the takeover flag, which only a finger sets, does take it ────────
def test_the_takeover_button_ends_the_desk_conversation_through_its_own_stop(voice_server):
    """A deliberate tap is allowed to move the conversation. The desk session is ended
    by calling ITS stop() — the same call a spoken sign-off makes, which is the path
    that persists the transcript — not by being discarded."""
    from core import floor

    server, sessions = voice_server
    desk = _desk_takes_the_floor()

    async def scenario():
        url = server.ws_url(TOKEN, str(uuid.uuid4())) + "&takeover=1"
        ws = await connect(url, max_size=None)
        client = Client(ws)
        await client.__aenter__()
        await client.expect("hello")
        await client.expect("audio")           # the conversation really opened
        assert client.of("busy") == []
        await _drain(ws, client)

    _run(scenario())

    assert desk.stopped == 1, "the takeover did not end the desk session at all"
    assert sessions and sessions[-1].fake is not None


# ── 3. the flag is not a mood: an ordinary reconnect can never carry it ──────
def test_only_an_explicit_flag_counts_as_a_takeover():
    """`_wants_takeover` is the entire safety property, so it is asserted directly:
    the server cannot tell "he walked to the sofa" from "the phone woke up in his
    pocket", and everything rests on the browser only setting this from a tap."""
    from server.app import _wants_takeover

    class FakeWS:
        def __init__(self, params):
            self.query_params = params

    assert _wants_takeover(FakeWS({"takeover": "1"})) is True
    assert _wants_takeover(FakeWS({"takeover": "true"})) is True
    for params in ({}, {"takeover": ""}, {"takeover": "0"}, {"takeover": "no"},
                   {"takeover": "maybe"}, {"Takeover": "1"}):
        assert _wants_takeover(FakeWS(params)) is False, params


def test_the_pwa_only_arms_takeover_inside_the_button_handler():
    """A static read of the shipped PWA. If a future edit sets TAKEOVER anywhere else —
    an auto-reconnect helper, a retry timer — the phone regains the ability to end
    Robin's desk conversation without him touching anything."""
    import pathlib

    html = (pathlib.Path(__file__).resolve().parent.parent
            / "server" / "index.html").read_text(encoding="utf-8")
    assigns = [line.strip() for line in html.splitlines()
               if "TAKEOVER =" in line and "let TAKEOVER" not in line]
    # Exactly two: armed in the button handler, spent (cleared) in connect().
    assert len(assigns) == 2, assigns
    assert "TAKEOVER = true;" in assigns[0] or "TAKEOVER = true;" in assigns[1]
    armed_at = html.index("TAKEOVER = true;")
    handler_at = html.index('$("takeover").onclick')
    assert armed_at > handler_at, "TAKEOVER is armed outside the button handler"


# ── 4. the floor is a SEAT, so the phone's own tabs do not fight ─────────────
def test_two_phone_tabs_share_one_claim_and_release_it_together(voice_server):
    """Per-tab isolation of a staged action is a different mechanism and stays intact
    (test_server_auth.py). What the floor must not do is refuse Robin's second tab, or
    hand the floor back while the first one is still talking."""
    from core import floor
    from server.app import PHONE_FLOOR_KEY

    server, _sessions = voice_server

    async def scenario():
        ws_a = await connect(server.ws_url(TOKEN, str(uuid.uuid4())), max_size=None)
        ws_b = await connect(server.ws_url(TOKEN, str(uuid.uuid4())), max_size=None)
        ca, cb = Client(ws_a), Client(ws_b)
        await ca.__aenter__()
        await cb.__aenter__()
        await ca.expect("audio")
        await cb.expect("audio")
        assert ca.of("busy") == [] and cb.of("busy") == []
        held = floor.holder()
        assert held is not None and held.key == PHONE_FLOOR_KEY

        # First tab leaves. The floor stays with the phone, because Robin is still on it.
        await ca.__aexit__()
        await ws_a.close()
        await await_for(lambda: len(__import__("server.app", fromlist=["SESSIONS"]).SESSIONS) == 1,
                        what="one tab left")
        assert floor.holder() is not None, "the floor was released while a tab was live"

        await cb.__aexit__()
        await ws_b.close()

    _run(scenario())

    from core import floor as f
    # Last tab out turns the light off, so the desk is free again.
    import time as _t
    deadline = _t.time() + 10
    while f.holder() is not None and _t.time() < deadline:
        _t.sleep(0.05)
    assert f.holder() is None, "the phone kept the floor after its last tab closed"


# ── 5. and the desk can get it back afterwards ───────────────────────────────
def test_the_desk_can_claim_once_the_phone_has_hung_up(voice_server):
    from core import floor

    server, _sessions = voice_server

    async def scenario():
        ws = await connect(server.ws_url(TOKEN, str(uuid.uuid4())), max_size=None)
        client = Client(ws)
        await client.__aenter__()
        await client.expect("audio")
        await client.__aexit__()
        await ws.close()

    _run(scenario())

    import time
    deadline = time.time() + 10
    while floor.holder() is not None and time.time() < deadline:
        time.sleep(0.05)
    holder = floor.claim(floor.DESK, "desk:menubar")
    assert holder.surface == floor.DESK
