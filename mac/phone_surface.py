"""The phone surface, as something the menubar can switch on and off.

This is the piece that makes `server/` a part of THIS Mac rather than a thing that was
once going to run on Thrivbe-1. It owns three resources and nothing else:

  1. a uvicorn serving `server.app:app` on **loopback**, in a daemon thread, so the
     AppKit main loop is never blocked;
  2. a `tailscale serve` handler that terminates TLS for it on a real `*.ts.net`
     certificate (`mac/tailnet.py` — HTTPS is not optional, `getUserMedia` refuses a
     plain-http origin and the microphone simply never opens);
  3. the shared token, minted into the Keychain the first time and shown to Robin so he
     can put it into the PWA once.

Order matters in both directions and is the reason this is a class rather than two
functions. On the way up: bind first, publish second — a `tailscale serve` handler
pointing at a port nothing answers is a URL that fails on the phone with no clue why.
On the way down: unpublish first, close second — the reverse leaves the tailnet
advertising a dead port, and Safari caches that failure hard enough that Robin will
think the feature is broken after it is fixed.

It is OFF by default (`phone_surface.enabled`). A listener on a laptop that nobody
asked for is not a feature.
"""
from __future__ import annotations

import secrets as _pysecrets
import threading
import time

from core import caps, config, floor, secrets
from mac import tailnet

# The name the token lives under, in the Keychain and in `VOICE_AGENT_TOKEN`. Must match
# `server.app._token`.
TOKEN_NAME = "voice_agent_token"

# Enough entropy that a tailnet peer guessing it is not the threat model. There is no
# rate limit on the socket (a lockout is a denial of service handed to whoever triggers
# it), so length is the whole brute-force budget — same reasoning as the reverse channel.
TOKEN_BYTES = 24


def ensure_token() -> str:
    """The phone's shared token, minting one into the Keychain if there is none.

    Minting is silent but not secret: it is shown in the menu and the surface refuses to
    start without one, so there is no state where a token exists and Robin cannot find
    it. Generating beats prompting here — a secret he invents is a secret he reuses.
    """
    existing = secrets.secret(TOKEN_NAME, ("VOICE_AGENT_TOKEN",))
    if existing:
        return existing
    minted = _pysecrets.token_urlsafe(TOKEN_BYTES)
    if not secrets.set_secret(TOKEN_NAME, minted):
        raise RuntimeError(
            "could not store the phone access token in the Keychain. Set "
            "VOICE_AGENT_TOKEN in the environment instead, or grant Keychain access.")
    caps.log("phone surface: minted a new access token into the Keychain")
    return minted


class PhoneSurface:
    """Robin's iPhone as a microphone and speaker for this process. One per app."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._server = None          # uvicorn.Server
        self._thread: threading.Thread | None = None
        self._url: str | None = None
        self._bind: str = ""
        self._port: int = 0
        self._tls_port: int = 0
        self.last_error: str | None = None

    # --- reading ------------------------------------------------------------
    # The readers below deliberately take NO lock. `_reconcile_phone_menu` calls
    # `status()` from the AppKit main thread every 0.3 s, and `stop()` holds the lock
    # while it joins uvicorn's thread — which is normally instant and is allowed to take
    # seconds. A menu repaint that can block on that is a beachball. Every field here is
    # a single attribute read, so the worst case is one tick showing a title that is
    # 300 ms stale, which is not a thing anyone can see.
    @property
    def is_on(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def url(self) -> str | None:
        return self._url

    def status(self) -> dict:
        """Everything the menu needs to render itself in one read."""
        held = floor.holder()
        return {
            "on": self.is_on,
            "url": self._url,
            "bind": self._bind,
            "port": self._port,
            "tls_port": self._tls_port,
            "error": self.last_error,
            "in_conversation": held is not None and held.surface == floor.PHONE,
        }

    # --- switching on -------------------------------------------------------
    def start(self) -> str:
        """Bring the surface up and return the https:// URL for the phone.

        Raises with a sentence Robin can act on — this is called from a menu click, so
        the failure has to be readable, not a traceback in a log he is not tailing.
        """
        with self._lock:
            if self.is_on:
                return self._url or ""
            self.last_error = None
            bind = tailnet.resolve_bind_host(config.get("phone_surface.bind", "127.0.0.1"))
            port = int(config.get("phone_surface.port", 8767) or 8767)
            tls_port = int(config.get("phone_surface.tls_port", 8443) or 8443)
            ensure_token()      # refuse to listen before there is anything to check
            try:
                self._serve_http(bind, port)
                # Publish only once the port answers. A serve handler in front of a
                # socket that is not up yet gives the phone a bare 502 and no clue.
                url = tailnet.serve_start(tls_port, port)
            except Exception as e:
                self._close_http()
                self.last_error = str(e)
                raise
            self._bind, self._port, self._tls_port, self._url = bind, port, tls_port, url
            caps.log(f"phone surface on: {url} -> http://{bind}:{port}")
            return url

    def _serve_http(self, bind: str, port: int) -> None:
        """uvicorn on a daemon thread. Imported here so the menubar app does not pay for
        FastAPI at launch when the surface is off (which is the default)."""
        import uvicorn

        from server.app import app as fastapi_app

        cfg = uvicorn.Config(fastapi_app, host=bind, port=port,
                             log_level="warning", lifespan="on")
        self._server = uvicorn.Server(cfg)
        # uvicorn installs signal handlers, which only the main thread may do; it
        # already tolerates the failure, but saying so here means the traceback never
        # appears in Robin's log looking like a real problem.
        self._server.install_signal_handlers = lambda: None
        self._thread = threading.Thread(target=self._server.run, daemon=True,
                                        name="phone-surface")
        self._thread.start()
        deadline = time.time() + 20
        while not getattr(self._server, "started", False):
            if time.time() > deadline or not self._thread.is_alive():
                raise RuntimeError(
                    f"the phone surface could not listen on {bind}:{port} — something "
                    f"else may already have that port.")
            time.sleep(0.02)

    # --- switching off ------------------------------------------------------
    def stop(self) -> None:
        """Take it off the tailnet, then off this machine. Idempotent.

        Any phone conversation in flight ends the way a spoken sign-off ends one: the
        app's lifespan shuts each session down through `LiveSession.stop()`, so the
        transcript is persisted by the normal path.
        """
        with self._lock:
            tls_port, self._tls_port = self._tls_port, 0
            if tls_port:
                if not tailnet.serve_stop(tls_port):
                    caps.log(f"phone surface: tailscale serve --https={tls_port} off failed "
                             f"— the tailnet may still advertise a dead port")
            self._close_http()
            self._url = None
            caps.log("phone surface off")

    def _close_http(self) -> None:
        server, self._server = self._server, None
        thread, self._thread = self._thread, None
        if server is not None:
            server.should_exit = True
        if thread is not None and thread.is_alive():
            thread.join(timeout=20)
            if thread.is_alive():
                caps.log("phone surface: uvicorn thread did not exit within 20s")

    def toggle(self) -> dict:
        """Menu action. Never raises: the menu shows `error` instead."""
        try:
            if self.is_on:
                self.stop()
                config.set_("phone_surface.enabled", False)
            else:
                self.start()
                config.set_("phone_surface.enabled", True)
        except Exception as e:      # noqa: BLE001 — a menu click must not kill the app
            self.last_error = str(e)
            caps.log(f"phone surface toggle failed: {e!r}")
            caps.notify(f"Phone surface: {e}")
        return self.status()
