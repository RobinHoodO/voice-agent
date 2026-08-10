"""The phone surface: one FastAPI app, one WebSocket, the same brain — literally.

It runs ON ROBIN'S MAC, inside the menubar app's process (`mac/phone_surface.py` starts
it), and it is reached over the tailnet:

    iPhone ──https──▶ tailscale serve (TLS, *.ts.net) ──▶ 127.0.0.1:8767 ──▶ this app

That is a change of MACHINE, not of shape. This package was written to run on Thrivbe-1
and host its own conversation there; Robin ruled on 2026-08-10 that there is one brain
and it is this Mac, so what survives is the transport (browser PCM in, model audio out)
and what went away is everything that assumed a second instance: the systemd/TLS-cert
deployment, and the startup that rewired `core.caps` process-wide. Both surfaces share
this process now, so the phone-ness is per SESSION (`server/session.py`) and never
process-wide.

What this is NOT, and never was: a second implementation of the conversation. `/live`
accepts browser mic PCM, feeds it into a real `core.live_session.LiveSession` through
`core.audio_core`'s mic pump, and streams the model's audio back down the same socket.
Every turn boundary, guard, watchdog and confirmation gate is the one core already runs
for the menubar; this file is a transport, an auth boundary, and the place the floor is
claimed.

**One brain, so one conversation — and that takes TWO mechanisms, not one.**

  * BETWEEN SEATS (desk vs phone) it is `core.floor`. A phone that connects while Robin
    is mid-sentence at his desk is REFUSED — his desk session is not touched — and told
    who has the floor.
  * WITHIN this surface (tab vs tab on the same phone) the floor cannot help: every tab
    claims the same `PHONE_FLOOR_KEY`, and the floor treats a re-claim on its own key as
    the same seat arriving again, which is exactly right for a seat and useless as a cap
    on conversations. So the cap lives here: `Conn.owns_conversation`, handed out by
    `_conversation_owner()` in the one synchronous stretch of `/live` that contains no
    `await`. A second tab gets a socket and a `busy` frame; it never calls
    `session.start()`, so it never opens a second paid realtime socket and never becomes
    a second brain answering into the same room.

Either refusal carries the same `busy` frame and therefore the same "Take over"
affordance in the PWA. `?takeover=1`, which only that button ever sets, is the deliberate
second act that ends the other conversation through its own `stop()`. The reasoning,
including why automatic takeover is the option that CAN lose a turn, is written out in
`core/floor.py`.

**Auth** is voice-bridge's proven pattern, unchanged in shape because it is the one that
has survived a year on Robin's phone:
  * a shared token — `X-Voice-Token` on HTTP, `?token=` on the WebSocket, because
    browsers cannot set custom headers on a WebSocket handshake;
  * a per-tab session UUID, which scopes ALL state. Each tab gets its own LiveSession
    object, so the one staged high-stakes action (`_pending_action`) lives on that
    object and a spoken "yes" in another tab cannot reach it. That isolation is
    structural, not a check — and `tests/test_server_auth.py` holds the line.
  * the token appears in the WebSocket URL, so uvicorn would log it;
    `_RedactTokenFilter` is lifted from voice-bridge for the same reason it exists there.

The listener binds loopback (`mac.tailnet.resolve_bind_host`, which refuses 0.0.0.0 by
rule). TLS is `tailscale serve`'s, because `getUserMedia` will not run on a plain-http
origin and a phone with no microphone is not a phone surface.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, WebSocket
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from core import config, floor, secrets
from core import caps
from server.audio_ws import BrowserAudioBridge
from server.session import BrowserLiveSession, ensure_capabilities

HERE = os.path.dirname(os.path.abspath(__file__))
RELEASE_PATH = os.path.join(HERE, "RELEASE")


def _max_sessions() -> int:
    """How many tabs may hold a socket at once.

    Sockets, not conversations. What caps CONVERSATIONS at one is
    `Conn.owns_conversation` (see `_conversation_owner`) — the floor does not, because
    every tab on this phone claims the same seat key. This number bounds only how many
    tabs may sit here at once; the surplus ones hold a socket and an unstarted
    `LiveSession` object, so they cost no provider connection and no thread. Read live
    from config so the menubar's setting applies without a restart.
    """
    env = os.getenv("VOICE_AGENT_MAX_SESSIONS")
    if env:
        try:
            return max(1, int(env))
        except ValueError:
            pass
    try:
        return max(1, int(config.get("phone_surface.max_sessions", 2) or 2))
    except (TypeError, ValueError):
        return 2


class _RedactTokenFilter(logging.Filter):
    """`/live` carries the shared token as a query param (browsers cannot set custom
    WebSocket headers), and uvicorn logs the raw request path straight to journald.
    The WebSocket "accepted" line comes from the uvicorn.error logger, not
    uvicorn.access — both are filtered so a version bump cannot silently start leaking
    the token. Lifted verbatim in intent from voice-bridge/server.py."""

    _TOKEN_RE = re.compile(r"(token=)[^&\s\"]+")

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            record.args = tuple(
                self._TOKEN_RE.sub(r"\1***", arg) if isinstance(arg, str) else arg
                for arg in record.args
            )
        if isinstance(record.msg, str):
            record.msg = self._TOKEN_RE.sub(r"\1***", record.msg)
        return True


for _name in ("uvicorn.access", "uvicorn.error"):
    logging.getLogger(_name).addFilter(_RedactTokenFilter())


def load_env(path: str | None = None) -> None:
    """Seed keys from Robin's workspace `.env` for a bare `uvicorn server.app:app` run.

    Inside the menubar app this is redundant — `mac/agent.py` has already done it, and
    `setdefault` means it cannot override what is there. It exists for the dev run,
    where nothing else has.
    """
    path = path or os.getenv("VOICE_AGENT_ENV",
                             os.path.expanduser("~/Thrivbe-AI/.env"))
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Make sure this Mac is registered in `core.caps`, then serve.

    `ensure_capabilities` is a no-op inside the menubar app (see `server/session.py`).
    Startup, not import time, is still the honest place for it: importing this module
    must never rewire anything.
    """
    ensure_capabilities()
    config.ensure_dirs()
    caps.log(f"phone surface up (release {_release()}, max {_max_sessions()} tabs)")
    yield
    for conn in list(SESSIONS.values()):
        await _shutdown(conn)
    SESSIONS.clear()
    floor.release(PHONE_FLOOR_KEY)


app = FastAPI(title="Thrivbe Voice — phone surface", lifespan=lifespan)

# session uuid -> live connection. Everything session-scoped hangs off this.
SESSIONS: dict[str, "Conn"] = {}

# Swappable so tests can drive the real LiveSession with a fake backend instead of
# opening a paid realtime connection. Production never touches it.
SESSION_FACTORY = BrowserLiveSession


# This whole surface's claim on the conversation floor (`core.floor`). ONE key for every
# tab, because the floor arbitrates SEATS: Robin's phone is one seat whether he has one
# tab open or three. Two OTHER questions are deliberately not this one's, and each has
# its own mechanism here:
#   * "may a second tab hold a second conversation?" — no: `Conn.owns_conversation`.
#   * "may a spoken yes in one tab confirm what another tab staged?" — no: a
#     `LiveSession` per tab, each with its own gate, unchanged.
# See `core/floor.py` for why these are separate questions.
PHONE_FLOOR_KEY = "phone:surface"


class Conn:
    """One authenticated tab: its bridge, its session, when it opened.

    `owns_conversation` is the per-surface half of "one brain, one conversation": true
    for the ONE tab whose `LiveSession` was started, false for every other tab holding a
    socket.

    It is only ever GRANTED on the server's event loop thread, inside a stretch of
    `/live` that contains no `await` — that is what makes check-then-claim atomic, and
    why no lock is needed. It may be CLEARED from another thread (`_evict_phone` runs on
    whichever thread took the floor, e.g. the AppKit thread on a desk double-tap); that
    direction is a plain bool store and can only ever make the invariant more true.
    """

    def __init__(self, key: str, bridge: BrowserAudioBridge, session):
        self.key = key
        self.bridge = bridge
        self.session = session
        self.opened_at = time.time()
        self.owns_conversation = False


def _token() -> str | None:
    """The client-facing shared secret.

    On this Mac that is the Keychain (`mac.keychain` registers the store), falling back
    to `VOICE_AGENT_TOKEN` for a dev run. `mac/phone_surface.py` mints one into the
    Keychain the first time Robin turns the surface on.
    """
    return secrets.secret("voice_agent_token", ("VOICE_AGENT_TOKEN",))


def _token_ok(candidate: str | None) -> bool:
    expected = _token()
    if not expected or not candidate:
        return False
    return hmac.compare_digest(str(candidate), str(expected))


def require_voice_token(x_voice_token: str = Header(default=None)):
    if not _token_ok(x_voice_token):
        raise HTTPException(status_code=401, detail="unauthorized")


def _release() -> str:
    try:
        with open(RELEASE_PATH, encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return "dev"


def _valid_session_key(key: str | None) -> bool:
    """A per-tab UUID. Rejecting anything else keeps the registry from being a
    free-form key/value store an attacker with the token could grow without bound."""
    if not key:
        return False
    try:
        uuid.UUID(str(key))
    except (ValueError, AttributeError, TypeError):
        return False
    return True


# ── static PWA ──────────────────────────────────────────────────────────────
def _file(name: str, media: str):
    path = os.path.join(HERE, name)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path, media_type=media)


@app.get("/")
def index():
    return _file("index.html", "text/html")


@app.get("/manifest.json")
def manifest():
    return _file("manifest.json", "application/manifest+json")


@app.get("/sw.js")
def service_worker():
    return _file("sw.js", "application/javascript")


@app.get("/icon-192.png")
def icon_192():
    return _file("icon-192.png", "image/png")


@app.get("/icon-512.png")
def icon_512():
    return _file("icon-512.png", "image/png")


@app.get("/ping")
def ping():
    """Unauthenticated liveness only — the deploy script curls this. No state leaks."""
    return PlainTextResponse("ok")


@app.get("/health")
def health(x_voice_token: str = Header(default=None)):
    if not _token_ok(x_voice_token):
        raise HTTPException(status_code=401, detail="unauthorized")
    now = time.time()
    held = floor.holder()
    return JSONResponse({
        "release": _release(),
        "sessions": len(SESSIONS),
        "max_sessions": _max_sessions(),
        "backend": (config.get("live.backend") or "openai"),
        # Who is talking to Pam right now, on either surface. The menubar reads the same
        # object; this is here so Robin can see it from the phone too.
        "floor": None if held is None else {
            "surface": held.surface,
            "age_s": round(held.age_s(), 1),
        },
        "live": [
            {
                "session": k[:8],
                "age_s": round(now - c.opened_at, 1),
                "mic_rate": c.bridge.mic_rate,
                "mic_frames_in": c.bridge.mic_frames_in,
                "audio_frames_out": c.bridge.audio_frames_out,
                "running": bool(getattr(c.session, "_running", False)),
                # Which tab holds the phone's one conversation. Every other tab here is
                # a socket showing a `busy` frame and nothing else.
                "talking": c.owns_conversation,
            }
            for k, c in list(SESSIONS.items())
        ],
    })


# ── the conversation ────────────────────────────────────────────────────────
FLOOR_BUSY_CLOSE = 4409     # private-use close code: "someone else has the floor"


def _wants_takeover(websocket: WebSocket) -> bool:
    """Did a HUMAN ask to take the conversation over?

    Only the PWA's "Take over" button ever sets this, and it clears it immediately after
    — an automatic reconnect (iOS restoring the app, a tunnel healing, the service worker
    reviving a socket) never carries it. That asymmetry is the whole safety property:
    without it, a phone waking up in Robin's pocket would be indistinguishable from
    Robin deciding to move to the sofa, and it would end his sentence at the desk.
    """
    return str(websocket.query_params.get("takeover", "")).lower() in ("1", "true", "yes")


def _end_tab(conn: "Conn", reason: str) -> None:
    """End one tab's conversation the normal way, and give up its conversation slot.

    `LiveSession.stop()` is the same call a spoken sign-off makes, so the transcript is
    persisted by the path that already persists them. The tab is told first, so the PWA
    can say what happened instead of showing a socket that went quiet.

    Synchronous on purpose, and that is load-bearing: `stop()` clears `_running` before
    it returns, so a caller may start the replacement conversation immediately without a
    window in which two are running.
    """
    conn.owns_conversation = False
    conn.bridge.send_json({"type": "ended", "reason": reason})
    try:
        conn.session.stop()
    except Exception as e:
        caps.log(f"phone session stop failed ({reason}): {e!r}")


def _evict_phone() -> None:
    """Robin took the conversation back at his desk. End every phone tab."""
    for conn in list(SESSIONS.values()):
        _end_tab(conn, "taken over at the desk")


def _conversation_owner(exclude: str | None = None) -> "Conn | None":
    """The tab that holds this phone's ONE conversation, if any.

    This is the per-surface cap the floor cannot provide (`PHONE_FLOOR_KEY` is one seat
    for every tab). Callers must not `await` between this returning None and setting
    `owns_conversation` on their own `Conn` — that unbroken stretch is the whole reason
    two tabs racing into `/live` cannot both be granted the conversation.
    """
    for key, conn in list(SESSIONS.items()):
        if key != exclude and conn.owns_conversation:
            return conn
    return None


@app.websocket("/live")
async def live(websocket: WebSocket):
    """One browser tab ↔ one LiveSession. Mic PCM up, model audio down."""
    token = websocket.query_params.get("token")
    session_key = websocket.query_params.get("session")
    if not _token_ok(token) or not _valid_session_key(session_key):
        # Accept first so a browser sees an explicit policy close (1008) rather than an
        # opaque handshake failure it cannot report.
        await websocket.accept()
        await websocket.close(code=1008)
        return

    if len(SESSIONS) >= _max_sessions() and session_key not in SESSIONS:
        await websocket.accept()
        await websocket.send_json({"type": "error", "text": "too many live sessions"})
        await websocket.close(code=1013)
        return

    await websocket.accept()

    takeover = _wants_takeover(websocket)

    # ── who is talking, before anything is built ─────────────────────────────
    # A refusal has to cost nothing: no bridge, no LiveSession, and above all no call
    # to `session.start()`, which is what opens the paid realtime socket.
    #
    # Everything from here to `conn.owns_conversation = True` runs without an `await`.
    # That is deliberate and it is the concurrency argument: the event loop cannot
    # interleave another `/live` between the question "is a tab already talking?" and
    # this tab's answer to it.
    rival = _conversation_owner(exclude=session_key)

    # The seat. Claimed carrying the session that is ACTUALLY in the conversation, so
    # `floor.session()` keeps pointing at it even when the arriving tab turns out to be
    # a bystander (below) — a background job must never be routed to a tab that is
    # sitting on a `busy` frame.
    claim = floor.take if takeover else floor.claim
    try:
        claim(floor.PHONE, PHONE_FLOOR_KEY,
              session=None if rival is None else rival.session,
              on_evict=_evict_phone)
    except floor.Busy as busy:
        # Robin is mid-conversation somewhere else. His conversation is NOT touched.
        caps.log(f"session {session_key[:8]} refused — {busy.holder.surface} has the floor")
        await websocket.send_json({
            "type": "busy",
            "holder": busy.holder.surface,
            "since_s": round(busy.holder.age_s(), 1),
            "text": busy.holder.describe(),
        })
        await websocket.close(code=FLOOR_BUSY_CLOSE)
        return

    # ── the conversation, which the seat does NOT bound ──────────────────────
    # Another tab on this same phone is talking. On iOS that is two taps away — the
    # home-screen PWA and a Safari tab are separate `sessionStorage` contexts, so they
    # are separate session UUIDs — and it must not become two brains in one room.
    if rival is not None and takeover:
        # A finger, on the Take-over button. Same act, same path as taking the floor
        # from the desk: the loser ends through its own stop(), which persists its
        # transcript and clears `_running` before it returns.
        caps.log(f"session {session_key[:8]} takes over from tab {rival.key[:8]}")
        _end_tab(rival, "taken over by another tab on this phone")
        rival = None

    # Same tab reconnecting (dropped mobile network): retire the old one first, so two
    # sessions never share a key — and never share a confirmation. Popped here (no
    # await) and shut down below, after this tab has taken its decision, so the gap
    # cannot be mistaken by a third tab for a free conversation slot.
    old = SESSIONS.pop(session_key, None)

    loop = asyncio.get_running_loop()
    bridge = BrowserAudioBridge(websocket, loop, session_key=session_key)
    session = SESSION_FACTORY(bridge=bridge, on_auto_stop=lambda: bridge.send_json(
        {"type": "ended", "reason": "auto-stop"}))
    conn = Conn(session_key, bridge, session)
    # THE line. `rival is None` was decided above with no `await` in between, so no
    # other tab can have been granted the conversation since. A bystander keeps its
    # socket (it is a tab Robin can still take over from) and gets its own LiveSession
    # object for confirmation isolation — but that object is never STARTED, so there is
    # no second realtime socket, no second mic pump and no second brain in the room.
    conn.owns_conversation = rival is None
    SESSIONS[session_key] = conn
    if conn.owns_conversation:
        # Attach the session to the claim we already hold, so a finished background task
        # can be offered to whoever Robin is actually talking to (`floor.session()`).
        floor.claim(floor.PHONE, PHONE_FLOOR_KEY, session=session, on_evict=_evict_phone)

    if old is not None:
        caps.log(f"session {session_key[:8]} reconnected — retiring the previous one")
        old.owns_conversation = False
        await _shutdown(old)

    # Everything from here is inside the try, so a failure in `start()` cannot leave
    # this connection in SESSIONS or leave the surface holding the floor.
    try:
        await websocket.send_json({"type": "hello", "session": session_key,
                                   "release": _release()})
        if conn.owns_conversation:
            session.start()
            caps.log(f"session {session_key[:8]} open ({len(SESSIONS)} live)")
        else:
            caps.log(f"session {session_key[:8]} is a bystander — "
                     f"tab {rival.key[:8]} holds the conversation")
            # The SAME frame the desk refusal sends, so the PWA renders the same
            # "Take over" affordance for both. Only the holder differs.
            await websocket.send_json({
                "type": "busy",
                "holder": floor.PHONE,
                "since_s": round(time.time() - rival.opened_at, 1),
                "text": ("Another tab on this phone is already in the conversation. "
                         "One brain — finish there, or take over here."),
            })

        tasks = [asyncio.ensure_future(_pump_browser(websocket, conn))]
        if conn.owns_conversation:
            # Nothing to watch on a bystander: its session was never started, so the
            # "session ended on its own" test would fire immediately and close a socket
            # Robin may still want to take over from.
            tasks.append(asyncio.ensure_future(_watch_session(conn)))
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for t in done:
            exc = t.exception()
            if exc:
                caps.log(f"session {session_key[:8]} task failed: {exc!r}")
    finally:
        was_owner = conn.owns_conversation
        conn.owns_conversation = False
        if SESSIONS.get(session_key) is conn:
            SESSIONS.pop(session_key, None)
        await _shutdown(conn)
        # The floor follows the CONVERSATION, not the socket count. Checked after the
        # pop and against the live registry, so (a) a tab that reconnected — its
        # replacement already owns the conversation under the same key — does not hand
        # the floor back on its predecessor's way out, and (b) a bystander tab left
        # staring at a busy frame cannot keep Robin's desk locked out.
        if _conversation_owner() is None:
            floor.release(PHONE_FLOOR_KEY)
            if was_owner:
                for other in list(SESSIONS.values()):
                    other.bridge.send_json({
                        "type": "notice",
                        "text": "The conversation is free — start one here when ready.",
                    })
        try:
            await websocket.close(code=1000)
        except Exception:
            pass
        caps.log(f"session {session_key[:8]} closed ({len(SESSIONS)} live)")


async def _pump_browser(websocket: WebSocket, conn: Conn) -> None:
    """Browser → session. Binary frames are mic PCM and go straight through.

    There is no buffering, batching or VAD here on purpose: the mic pump in
    `core.audio_core` needs EVERY frame, including the ones inside the barge-in hold
    window, or it loses the first word of an interruption.
    """
    session = conn.session
    while True:
        frame = await websocket.receive()
        if frame.get("type") == "websocket.disconnect":
            return
        data = frame.get("bytes")
        if data is not None:
            # The one and only reason a frame is ever dropped here, and it is not a
            # turn-boundary decision: this tab holds no conversation (another tab on
            # this phone does), so there is no started session to feed and its mic
            # queue would just grow. The PWA of a bystander never gets the `audio`
            # config frame and so sends nothing anyway; this is the belt.
            if conn.owns_conversation:
                conn.bridge.feed_mic(session, data)
            continue
        text = frame.get("text")
        if not text:
            continue
        try:
            control = json.loads(text)
        except Exception:
            caps.log("ignoring invalid control JSON from the browser")
            continue
        kind = control.get("type")
        if kind == "stop":
            caps.log(f"session {conn.key[:8]}: browser asked to stop")
            return
        elif kind == "ping":
            conn.bridge.send_json({"type": "pong"})
        # Anything else is deliberately ignored: turn boundaries are core's job, so a
        # client-sent activity_start/activity_end must never reach the backend.


async def _watch_session(conn: Conn) -> None:
    """Close the socket when the session ends on its own — the idle/max watchdogs in
    core, a spoken sign-off, or an unrecoverable backend error."""
    while True:
        await asyncio.sleep(0.4)
        if not getattr(conn.session, "_running", False):
            conn.bridge.send_json({"type": "ended", "reason": "session closed"})
            await asyncio.sleep(0.1)   # let the frame leave before the socket dies
            return


async def _shutdown(conn: Conn) -> None:
    """Stop one tab's session and join its thread without blocking the app's event loop.

    Deliberately does NOT touch the floor: the claim belongs to the surface, not to this
    connection, and the caller releases it when the registry is empty.
    """
    conn.bridge.close()
    try:
        conn.session.stop()
    except Exception as e:
        caps.log(f"session stop failed: {e!r}")
    thread = getattr(conn.session, "_thread", None)
    if thread is not None and thread.is_alive():
        try:
            await asyncio.get_running_loop().run_in_executor(None, thread.join, 8.0)
        except Exception as e:
            caps.log(f"session join failed: {e!r}")
        if thread.is_alive():
            caps.log(f"session {conn.key[:8]} thread did not exit within 8s")
