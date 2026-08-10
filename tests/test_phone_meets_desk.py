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


# ── 4. one phone, two tabs: one seat, and — separately — ONE conversation ────
def _running(sessions):
    return [s for s in sessions if getattr(s, "_running", False)]


def _reached_the_model(sessions):
    """Sessions that actually opened a conversation with a provider.

    `RecordedSession._make_backend` runs on the session thread, so a session that was
    never started has no `fake` at all; `fake.setup` is set by `send_setup`, which is
    the first thing a real conversation does. Either half alone would be weaker: the
    attribute proves no backend was built, the setup proves none was configured.
    """
    out = []
    for s in sessions:
        fake = getattr(s, "fake", None)
        if fake is not None and fake.setup is not None:
            out.append(s)
    return out


def test_two_phone_tabs_share_one_claim_and_release_it_together(voice_server):
    """The floor keys this whole surface as ONE seat, so it cannot be what stops two
    tabs from running two conversations — and for a while nothing did. Two tabs on one
    phone meant two `LiveSession`s started, two paid realtime sockets, and two brains
    answering into the same room off the same speaker.

    So: the second tab is still accepted (it is Robin's own phone, and he must be able
    to take over from it), it still gets its own LiveSession object for confirmation
    isolation — but it is NOT started, and it is told who is talking.
    """
    from core import floor
    from server.app import SESSIONS, PHONE_FLOOR_KEY

    server, sessions = voice_server

    async def scenario():
        ws_a = await connect(server.ws_url(TOKEN, str(uuid.uuid4())), max_size=None)
        ca = Client(ws_a)
        await ca.__aenter__()
        await ca.expect("audio")                    # tab A is in the conversation

        ws_b = await connect(server.ws_url(TOKEN, str(uuid.uuid4())), max_size=None)
        cb = Client(ws_b)
        await cb.__aenter__()
        await cb.expect("hello")                    # not refused: a real socket
        busy = await cb.expect("busy")
        assert busy["holder"] == "phone", busy
        assert cb.of("audio") == [], "the second tab was given a conversation"

        # THE invariant. One phone, one brain, whatever the tab count.
        await await_for(lambda: len(SESSIONS) == 2, what="both tabs registered")
        assert len(_running(sessions)) == 1, \
            f"{len(_running(sessions))} conversations running on one phone"
        assert len(_reached_the_model(sessions)) == 1, \
            "a second tab opened its own provider socket"
        assert len(sessions) == 2, "the bystander must still get its own session object"

        held = floor.holder()
        assert held is not None and held.key == PHONE_FLOOR_KEY
        assert held.session is _running(sessions)[0], \
            "the floor points at a tab that is not the one talking"

        # The bystander leaves. The floor stays: Robin is still talking in tab A.
        await cb.__aexit__()
        await ws_b.close()
        await await_for(lambda: len(SESSIONS) == 1, what="the bystander left")
        assert floor.holder() is not None, "the floor was released while a tab was live"

        await ca.__aexit__()
        await ws_a.close()

    _run(scenario())

    from core import floor as f
    # Last tab out turns the light off, so the desk is free again.
    import time as _t
    deadline = _t.time() + 10
    while f.holder() is not None and _t.time() < deadline:
        _t.sleep(0.05)
    assert f.holder() is None, "the phone kept the floor after its last tab closed"


def test_a_second_tab_takes_the_conversation_over_only_when_asked(voice_server):
    """The bystander's way out is the same deliberate act the desk case has: the
    Take-over button, and nothing automatic. The loser ends through its own `stop()`,
    so its transcript is persisted, and the count of running conversations never goes
    above one on the way through."""
    from server.app import SESSIONS

    server, sessions = voice_server
    key_b = str(uuid.uuid4())

    async def scenario():
        ws_a = await connect(server.ws_url(TOKEN, str(uuid.uuid4())), max_size=None)
        ca = Client(ws_a)
        await ca.__aenter__()
        await ca.expect("audio")

        # Same tab id as the bystander would use, now carrying a human's decision.
        ws_b = await connect(server.ws_url(TOKEN, key_b) + "&takeover=1", max_size=None)
        cb = Client(ws_b)
        await cb.__aenter__()
        await cb.expect("audio")                     # B is now the conversation
        assert cb.of("busy") == []
        ended = await ca.expect("ended")             # A was told, not just dropped
        assert "another tab" in ended["reason"]

        await await_for(lambda: len(_running(sessions)) == 1,
                        what="exactly one conversation after the takeover")
        assert _running(sessions)[0] is sessions[-1], "the wrong tab kept the floor"
        await await_for(lambda: SESSIONS.get(key_b) is not None
                        and SESSIONS[key_b].owns_conversation,
                        what="the new tab owns the conversation")

        await ca.__aexit__()
        await cb.__aexit__()
        await ws_a.close()
        await ws_b.close()

    _run(scenario())
    assert len(_running(sessions)) == 0


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
