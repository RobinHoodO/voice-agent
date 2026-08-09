"""One conversation, wired to one browser tab.

`BrowserLiveSession` is `core.live_session.LiveSession` with four surface hooks
overridden — state, barge-in, notifications, transcript. It adds no conversation
logic of its own: the VAD, the turn boundaries, the guards, the watchdogs and the
tool gate all stay in core, identical to the Mac.

The one that matters is `_flush_out`. On the Mac barge-in means "drop the queued
chunks and the speaker goes quiet within ~100 ms". Over a socket the phone is holding
its own buffer, so dropping the server queue is not enough — the browser has to be
TOLD. `_flush_out` is exactly the point core already calls at barge-in
(`_on_speech_started` → `_flush_out()` → `cancel_response()`), so hooking it keeps the
browser in lockstep with the Mac instead of inventing a second interruption path.
"""
from __future__ import annotations

from core import caps
from core.live_session import LiveSession
from server.audio_ws import BrowserAudioBridge

# Which capability profile this surface runs (see core/capabilities.py): no clipboard,
# no herdr lanes, no screen — and a shell on Thrivbe-1 whose destructive commands stage
# through the spoken confirmation gate. Declared at module level so
# `check_tool_drift.py` can read it without importing the server package.
SURFACE_PROFILE = "server"


class BrowserLiveSession(LiveSession):
    """A LiveSession whose speaker, microphone and status light are a browser tab."""

    # Structural, not a check: there is no way to get a browser session that is not on
    # the server profile, because this is the class the server surface instantiates.
    # `install_capabilities()` registering the same name is belt to this braces — a
    # session built before startup finished would still be gated correctly.
    PROFILE = SURFACE_PROFILE

    def __init__(self, bridge: BrowserAudioBridge, **kwargs):
        # Set before super().__init__: the transport reads `_bridge` off the session,
        # and _push_state can fire as soon as the session thread starts.
        self._bridge = bridge
        kwargs.setdefault("on_state", self._push_state)
        super().__init__(**kwargs)

    # --- surface hooks ------------------------------------------------------
    def _push_state(self, state: str) -> None:
        """idle / listening / speaking / thinking / acting / reconnecting."""
        self._bridge.send_json({"type": "state", "state": state})

    def _flush_out(self) -> None:
        """Barge-in. Drop the server-side queue (core), then tell the tab to drop its
        own playback buffer.

        A chunk already in flight still lands — same ~100 ms straggler the Mac plays
        out of the speaker mid-write. Parity, not a bug: the browser clears its ring on
        this frame and the straggler is the tail of a chunk that was already audible.
        """
        super()._flush_out()
        self._bridge.send_json({"type": "interrupted"})

    def _notify(self, msg: str) -> None:
        super()._notify(msg)      # process-level sink (journald)
        self._bridge.send_json({"type": "notice", "text": msg})

    def _record_turn(self, turn: str) -> None:
        super()._record_turn(turn)   # in-memory + fsync'd journal, unchanged
        role, _, text = turn.partition(": ")
        self._bridge.send_json({"type": "turn", "role": role, "text": text})


def install_capabilities(log_sink=None) -> None:
    """Register the server surface's capabilities into `core.caps`.

    Deliberately partial. There is no screen and no clipboard on Thrivbe-1, so
    `caps.screen()` and `caps.clipboard()` keep their null implementations and the
    tools that use them degrade to "(no context)" / "no clipboard on this surface" —
    which is what fail-soft was built for. Secrets need nothing: `core.secrets` already
    resolves env → `$CREDENTIALS_DIRECTORY`, which is what the systemd unit provides.
    """
    caps.set_profile(SURFACE_PROFILE)
    caps.set_log_sink(log_sink or _print_log)
    caps.set_notifier(_notify_log)
    from server import audio_ws
    audio_ws.install()


def _print_log(msg: str) -> None:
    print(f"[voice] {msg}", flush=True)


def _notify_log(msg: str) -> None:
    print(f"[voice][notice] {msg}", flush=True)
