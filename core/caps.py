"""Capability registry — the one seam between the brain and whatever surface runs it.

`core` never imports AppKit/Quartz/sounddevice/rumps, and never shells out to
`osascript`. Everything that needs a real machine is declared here as a small
interface with a **null implementation that fails soft**, and the surface swaps in
a real one at startup (`mac.caps_install.install()` on this Mac).

Fail-soft is deliberate and matches the code this replaced: every one of these was
previously a `try: <mac thing> except Exception: pass`. A missing capability must
degrade the answer, never kill the conversation.
"""
from __future__ import annotations


# --- log --------------------------------------------------------------------
# Was: `try: from agent import LOG; LOG(msg) except Exception: pass` repeated in
# eleven modules — an import-time dependency on the macOS menubar app.
_log_sink = None


def set_log_sink(fn) -> None:
    """Route core's diagnostic lines somewhere. `fn(msg: str) -> None`."""
    global _log_sink
    _log_sink = fn


def log(msg: str) -> None:
    fn = _log_sink
    if fn is None:
        return
    try:
        fn(msg)
    except Exception:
        pass


# --- user-visible notification ---------------------------------------------
_notifier = None


def set_notifier(fn) -> None:
    """`fn(msg: str) -> None` — a desktop notification, a push, a log line."""
    global _notifier
    _notifier = fn


def notify(msg: str) -> None:
    fn = _notifier
    if fn is None:
        return
    try:
        fn(msg)
    except Exception:
        pass


# --- screen context ---------------------------------------------------------
class ScreenContext:
    """What the user is looking at. Mac: AX text + cursor text + a window JPEG."""

    def grab_context(self) -> str:
        return "(no context)"

    def grab_window_screenshot(self) -> str:
        return ""


_screen = ScreenContext()


def set_screen(impl: ScreenContext) -> None:
    global _screen
    _screen = impl


def screen() -> ScreenContext:
    return _screen


# --- clipboard / typing into the front window -------------------------------
class Clipboard:
    """Put text where the user can use it. Mac: pbcopy + a synthetic Cmd-V."""

    def put_text(self, text: str, paste: bool = True) -> str:
        return "no clipboard on this surface"


_clipboard = Clipboard()


def set_clipboard(impl: Clipboard) -> None:
    global _clipboard
    _clipboard = impl


def clipboard() -> Clipboard:
    return _clipboard


# --- audio transport --------------------------------------------------------
class AudioTransport:
    """Devices and streams. The VAD / turn-boundary logic is NOT here — that is
    `core.audio_core.AudioCoreMixin`, so a browser-mic transport reuses it whole.

    A transport owns exactly three things for a session `s`:
      * opening the capture stream and feeding `s._mic_cb(...)`
      * a player that drains `s._out_q`
      * bringing both to rest (`teardown`) before anything re-inits the audio stack
    """

    def start(self, s) -> None:
        raise RuntimeError("no audio transport registered for this surface")

    def player(self, s) -> None:
        """Drain `s._out_q` to the speaker until `s._audio_stop`. `start` owns the thread."""

    def teardown(self, s) -> None:
        pass


class NullAudioTransport(AudioTransport):
    def start(self, s) -> None:
        raise RuntimeError("no audio transport registered for this surface")


_audio_transport: AudioTransport = NullAudioTransport()


def set_audio_transport(impl: AudioTransport) -> None:
    global _audio_transport
    _audio_transport = impl


def audio_transport() -> AudioTransport:
    return _audio_transport
