"""The phone surface: one FastAPI app, one WebSocket, the same brain as the Mac.

    /opt/voice-agent/bin/uvicorn server.app:app --host 0.0.0.0 --port 8767 \
        --ssl-keyfile /opt/voice-bridge/certs/cert.key \
        --ssl-certfile /opt/voice-bridge/certs/cert.crt

What this is NOT: a second implementation of the conversation. `/live` accepts browser
mic PCM, feeds it into a real `core.live_session.LiveSession` through
`core.audio_core`'s mic pump, and streams the model's audio back down the same socket.
Every turn boundary, guard, watchdog and confirmation gate is the one core already runs
on the Mac; this file is a transport and an auth boundary.

**Auth** is voice-bridge's proven pattern, unchanged in shape because it is the one that
has survived a year on Robin's phone:
  * a shared token — `X-Voice-Token` on HTTP, `?token=` on the WebSocket, because
    browsers cannot set custom headers on a WebSocket handshake;
  * a per-tab session UUID, which scopes ALL state. Each tab gets its own LiveSession
    object, so the one staged high-stakes action (`_pending_action`) lives on that
    object and a spoken "yes" in another tab cannot reach it. That isolation is
    structural, not a check — and `tests/test_server_auth.py` holds the line.
  * the token appears in the WebSocket URL, so uvicorn would log it to journald;
    `_RedactTokenFilter` is lifted from voice-bridge for the same reason it exists there.
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

from core import caps, config, secrets
from server.audio_ws import BrowserAudioBridge
from server.session import BrowserLiveSession, install_capabilities

HERE = os.path.dirname(os.path.abspath(__file__))
RELEASE_PATH = os.path.join(HERE, "RELEASE")

# How many phones/tabs may hold a live model connection at once. Each one is a paid
# realtime session and its own thread — small on purpose.
MAX_SESSIONS = int(os.getenv("VOICE_AGENT_MAX_SESSIONS", "4"))


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
    """Read `/opt/voice-agent/.env` if it exists. Server-only file, never deployed."""
    path = path or os.getenv("VOICE_AGENT_ENV", "/opt/voice-agent/.env")
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
    """Register the surface's capabilities once, when the app actually starts.

    Import-time registration would make merely importing this module rewire
    `core.caps` process-wide — which is exactly the coupling the core/mac split
    removed. Startup is the honest place.
    """
    install_capabilities()
    config.ensure_dirs()
    caps.log(f"voice-agent server up (release {_release()}, max {MAX_SESSIONS} sessions)")
    yield
    for conn in list(SESSIONS.values()):
        await _shutdown(conn)
    SESSIONS.clear()


app = FastAPI(title="Thrivbe Voice — phone surface", lifespan=lifespan)

# session uuid -> live connection. Everything session-scoped hangs off this.
SESSIONS: dict[str, "Conn"] = {}

# Swappable so tests can drive the real LiveSession with a fake backend instead of
# opening a paid realtime connection. Production never touches it.
SESSION_FACTORY = BrowserLiveSession


class Conn:
    """One authenticated tab: its bridge, its session, when it opened."""

    def __init__(self, key: str, bridge: BrowserAudioBridge, session):
        self.key = key
        self.bridge = bridge
        self.session = session
        self.opened_at = time.time()


def _token() -> str | None:
    """The client-facing shared secret. Keychain is absent here, so this resolves
    env → `$CREDENTIALS_DIRECTORY/voice_agent_token` (what the systemd unit provides)."""
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
    return JSONResponse({
        "release": _release(),
        "sessions": len(SESSIONS),
        "max_sessions": MAX_SESSIONS,
        "backend": (config.get("live.backend") or "openai"),
        "live": [
            {
                "session": k[:8],
                "age_s": round(now - c.opened_at, 1),
                "mic_rate": c.bridge.mic_rate,
                "mic_frames_in": c.bridge.mic_frames_in,
                "audio_frames_out": c.bridge.audio_frames_out,
                "running": bool(getattr(c.session, "_running", False)),
            }
            for k, c in list(SESSIONS.items())
        ],
    })


# ── the conversation ────────────────────────────────────────────────────────
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

    if len(SESSIONS) >= MAX_SESSIONS and session_key not in SESSIONS:
        await websocket.accept()
        await websocket.send_json({"type": "error", "text": "too many live sessions"})
        await websocket.close(code=1013)
        return

    await websocket.accept()

    # Same tab reconnecting (dropped mobile network): retire the old one first, so two
    # sessions never share a key — and never share a confirmation.
    old = SESSIONS.pop(session_key, None)
    if old is not None:
        caps.log(f"session {session_key[:8]} reconnected — retiring the previous one")
        await _shutdown(old)

    loop = asyncio.get_running_loop()
    bridge = BrowserAudioBridge(websocket, loop, session_key=session_key)
    session = SESSION_FACTORY(bridge=bridge, on_auto_stop=lambda: bridge.send_json(
        {"type": "ended", "reason": "auto-stop"}))
    conn = Conn(session_key, bridge, session)
    SESSIONS[session_key] = conn

    await websocket.send_json({"type": "hello", "session": session_key,
                               "release": _release()})
    session.start()
    caps.log(f"session {session_key[:8]} open ({len(SESSIONS)} live)")

    recv = asyncio.ensure_future(_pump_browser(websocket, conn))
    watch = asyncio.ensure_future(_watch_session(conn))
    try:
        done, pending = await asyncio.wait([recv, watch],
                                           return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for t in done:
            exc = t.exception()
            if exc:
                caps.log(f"session {session_key[:8]} task failed: {exc!r}")
    finally:
        if SESSIONS.get(session_key) is conn:
            SESSIONS.pop(session_key, None)
        await _shutdown(conn)
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
    """Stop the session and join its thread without blocking the app's event loop."""
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
