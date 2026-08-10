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
from server import audio_ws
from server.audio_ws import BrowserAudioBridge

# Which capability profile this surface runs (see core/capabilities.py). This process
# is Mac-Pam — the same brain, on Robin's Mac — and this package is only the transport
# that puts its microphone and speaker in a browser tab on his phone. So the profile is
# `phone`, named for the SEAT rather than for a machine: no screen he can see, no window
# to paste into, and no `run_shell` at all. The herdr lanes and every kernel tool stay,
# because they run here on the Mac. Declared at module level so `check_tool_drift.py`
# can read it without importing the server package.
SURFACE_PROFILE = "phone"


class BrowserLiveSession(LiveSession):
    """A LiveSession whose speaker, microphone and status light are a browser tab."""

    # Structural, not a check: there is no way to get a browser session that is not on
    # the phone profile, because this is the class the browser surface instantiates.
    #
    # THREE things are per-session here, and the list is the whole contract for adding a
    # fourth surface to this process. Anything that used to be "what this process is"
    # and is now "which seat is asking" has to appear here, or in something this pins:
    #
    #   1. PROFILE          — which tools exist, and (via `capabilities`) which of the
    #                         machine's senses this seat may use.
    #   2. AUDIO_TRANSPORT  — whose microphone and speaker (below).
    #   3. SCREEN CONTEXT   — whether the Mac's focused window, cursor text and
    #                         screenshot reach the model. Not pinned here, because it is
    #                         DERIVED from PROFILE at the point of use
    #                         (`LiveSession._may_read_the_screen` →
    #                         `capabilities.has_screen_context`) — but it is a third
    #                         thing that has to be per-session, and it was the one that
    #                         got missed. Until 2026-08-10 the grab was gated only on the
    #                         process-wide `privacy.*` toggles, which described the
    #                         MACHINE; on Thrivbe-1 there was no Mac screen to grab, so
    #                         the impossibility was structural. Moving the brain into the
    #                         menubar process turned it into an unenforced promise, and
    #                         the phone was handed Robin's screen on every turn while its
    #                         own prompt said it could not see one.
    #
    # PROFILE is also the ONLY thing that makes a session the phone, and that is
    # deliberate. The surface used to also call `caps.set_profile("phone")` at startup,
    # which was right when this package was the only thing in its process on Thrivbe-1
    # and is actively wrong now: it runs inside the menubar app, where the process-wide
    # profile is `mac` and has to stay `mac` for Robin's own desk conversation and for
    # `core.shell`. Per-session, or it is a bug.
    PROFILE = SURFACE_PROFILE

    # This session's speaker and microphone are a WebSocket, not PortAudio. Pinned here
    # for exactly the same reason as PROFILE — the menubar surface in this process keeps
    # `core.caps`' PortAudio transport, and neither may reroute the other.
    AUDIO_TRANSPORT = audio_ws.transport()

    def __init__(self, bridge: BrowserAudioBridge, **kwargs):
        # Set before super().__init__: the transport reads `_bridge` off the session,
        # and _push_state can fire as soon as the session thread starts.
        self._bridge = bridge
        kwargs.setdefault("on_state", self._push_state)
        super().__init__(**kwargs)

    # --- surface hooks ------------------------------------------------------
    async def _configure(self) -> None:
        """Hand the tab its audio format BEFORE core builds the prompt.

        Core's order is connect → `_configure` → `_start_audio`, and `_start_audio` is
        where the transport normally announces the format. On the Mac that is invisible:
        PortAudio is already open. In a browser it is the gate on the microphone — the
        tab cannot resample without knowing the rate, so it drops every frame until the
        `audio` frame lands — and `_configure` is the slow step (kernel persona, memory
        recall, the high-stakes manifest: 2.3 s measured over the tailnet). That was the
        first sentence of every conversation, gone.

        Nothing downstream moves: the frames the tab now sends during `_configure` land
        in the session's mic queue (`_mic_cb` gates on `_audio_stop`, which is clear on a
        fresh attempt) and are pumped the moment `_pump_mic` starts — so the words are
        queued rather than dropped, and the backend has been set up before any of them
        reach it.
        """
        self._transport().announce(self)
        await super()._configure()

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


def ensure_capabilities() -> None:
    """Make sure THIS MAC is registered in `core.caps` — and change nothing if it is.

    This surface no longer installs "the phone" into the process, because there is no
    such machine: the phone is a seat, and the machine under every seat is this Mac
    (Robin, 2026-08-10). What the phone-ness narrows is per-session — `PROFILE` and
    `AUDIO_TRANSPORT` above — and what `core.caps` answers is "how do I reach the
    machine", which has one true answer here whoever is asking.

    So the normal case (running inside the menubar app, where `mac.caps_install.install`
    already ran) is a NO-OP, deliberately: rewiring the log sink or the audio transport
    out from under Robin's own desk conversation is the exact bug this replaces.

    The other case is a bare `uvicorn server.app:app` for development. Then nothing is
    registered, the strict `unknown` profile is in force, and `core.shell` would pick a
    shell by guesswork — so we install the Mac's capabilities, because that is the
    machine this process is on either way.
    """
    if caps.profile() is not None:
        return
    try:
        from mac import caps_install
        caps_install.install(log_sink=_print_log)
    except Exception as e:      # noqa: BLE001 — a dev run without PyObjC still serves
        caps.set_log_sink(_print_log)
        caps.set_notifier(_notify_log)
        caps.log(f"phone surface: macOS capabilities unavailable ({e!r}) — "
                 f"running with core's fail-soft defaults")


def _print_log(msg: str) -> None:
    print(f"[voice] {msg}", flush=True)


def _notify_log(msg: str) -> None:
    print(f"[voice][notice] {msg}", flush=True)
