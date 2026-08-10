"""The phone surface must not reroute the conversation Robin is having at his desk.

This is the bug that had to be fixed for `server/` to live on the Mac at all, and it is
invisible from either surface on its own. When this package was going to run alone in a
process on Thrivbe-1, `install_capabilities()` calling `caps.set_profile("phone")` and
`caps.set_audio_transport(browser)` was correct. In the menubar's process those two
lines mean: the moment Robin turns the phone surface on, his DESK session's next audio
teardown/restart resolves the browser transport and his microphone stops existing, and
`core.shell` starts asking the phone profile which binary to spawn.

So `core.caps` answers only "what machine is this" — one true answer here, whoever is
asking — and everything that differs between the seats is per session.
"""
import queue
import threading

import pytest

from core import caps, capabilities
from core.live_session import LiveSession
from server import audio_ws
from server.session import BrowserLiveSession, ensure_capabilities


class SentinelTransport(caps.AudioTransport):
    """Stands in for `mac.audio`'s PortAudio transport."""

    def __init__(self):
        self.started = []

    def start(self, s):
        self.started.append(s)


@pytest.fixture
def mac_owns_caps(monkeypatch):
    """The process as it is inside the menubar app: profile `mac`, PortAudio installed."""
    transport = SentinelTransport()
    monkeypatch.setattr(caps, "_profile", "mac", raising=False)
    monkeypatch.setattr(caps, "_audio_transport", transport, raising=False)
    return transport


# ── the per-session split ────────────────────────────────────────────────────
def test_a_browser_session_uses_the_browser_transport(mac_owns_caps):
    """Even though the process-wide transport is the Mac's."""
    session = BrowserLiveSession.__new__(BrowserLiveSession)
    assert session._transport() is audio_ws.transport()
    assert session._transport() is not mac_owns_caps


def test_a_desk_session_still_uses_the_machines_transport(mac_owns_caps):
    """A plain LiveSession — what `mac/agent.py::toggle_live` builds — must be
    completely unaffected by the phone surface existing."""
    session = LiveSession.__new__(LiveSession)
    assert session.AUDIO_TRANSPORT is None
    assert session._transport() is mac_owns_caps


def test_a_browser_session_is_the_phone_profile_and_a_desk_session_is_not(mac_owns_caps):
    browser = BrowserLiveSession.__new__(BrowserLiveSession)
    desk = LiveSession.__new__(LiveSession)
    assert browser.profile_name == "phone"
    assert desk.profile_name == "mac"
    # …and the narrowing follows from the profile, not from where the code lives.
    assert capabilities.SHELL_TOOL in capabilities.excluded_tools(browser.profile_name)
    assert capabilities.SHELL_TOOL not in capabilities.excluded_tools(desk.profile_name)


def test_the_two_transports_reach_different_sessions_side_by_side(mac_owns_caps):
    """Both surfaces live in one process, so `_start_audio` has to route per session
    rather than per process. Driven through the real entry point core calls."""
    class FakeBackend:
        mic_rate = 16000
        manual_vad = True

    class Bridge:
        session_key = "abcdef12"
        mic_rate = 0
        audio_config = None      # the format this tab was last told, per BrowserAudioBridge

        def __init__(self):
            self.sent = []

        def send_json(self, obj):
            self.sent.append(obj)

    browser = BrowserLiveSession.__new__(BrowserLiveSession)
    browser._backend = FakeBackend()
    browser._bridge = Bridge()
    browser._audio_stop = threading.Event()
    browser._out_q = queue.Queue()
    browser._running = False
    browser._player_thread = None
    browser._start_audio()
    browser._player_thread.join(timeout=5)

    desk = LiveSession.__new__(LiveSession)
    desk._start_audio()

    assert browser._bridge.sent and browser._bridge.sent[0]["type"] == "audio"
    assert mac_owns_caps.started == [desk], "the phone's audio went to the Mac's devices"


# ── the startup hook is a no-op where it used to be a hijack ─────────────────
def test_ensure_capabilities_changes_nothing_when_the_mac_is_registered(mac_owns_caps):
    before_profile, before_transport = caps.profile(), caps.audio_transport()
    before_log, before_notify = caps._log_sink, caps._notifier

    ensure_capabilities()

    assert caps.profile() == before_profile == "mac"
    assert caps.audio_transport() is before_transport is mac_owns_caps
    assert caps._log_sink is before_log
    assert caps._notifier is before_notify


def test_ensure_capabilities_installs_this_mac_when_nothing_is_registered(monkeypatch):
    """The bare `uvicorn server.app:app` dev run. It is still on Robin's Mac, so the
    machine's capabilities are the Mac's — NOT a "phone machine", which does not exist.
    Falling through to the strict `unknown` profile would have `core.shell` guessing."""
    monkeypatch.setattr(caps, "_profile", None, raising=False)
    monkeypatch.setattr(caps, "_audio_transport", caps._audio_transport, raising=False)
    monkeypatch.setattr(caps, "_log_sink", caps._log_sink, raising=False)
    monkeypatch.setattr(caps, "_notifier", caps._notifier, raising=False)

    ensure_capabilities()

    assert caps.profile() == "mac", (
        "a dev run left the process on a profile that is not this machine")


def test_the_browser_transport_is_not_installable_process_wide():
    """`audio_ws.install()` is gone on purpose — it was the hijack. If someone adds it
    back, this fails and they get to read why in the module docstring."""
    assert not hasattr(audio_ws, "install")


def test_the_surface_never_rewires_the_process():
    """Read statically — the damage happens at import/startup, in a process this test
    cannot easily observe from the inside.

    Parsed rather than grepped, deliberately: both names appear in `server/`'s prose,
    explaining why they are NOT called, and a substring check that fails on its own
    explanation is a test people delete.
    """
    import ast
    import pathlib

    banned = {"set_profile", "set_audio_transport"}
    server_dir = pathlib.Path(__file__).resolve().parent.parent / "server"
    called: dict[str, str] = {}
    for path in sorted(server_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name in banned:
                called[f"{path.name}:{node.lineno}"] = name
    assert not called, (
        "server/ rewires core.caps process-wide: " + repr(called) + ". Both surfaces "
        "share this process now, so that reroutes the menubar's own conversation — pin "
        "PROFILE / AUDIO_TRANSPORT on the session class instead.")


# ── and the whole server, running, leaves the process alone ──────────────────
def test_running_the_phone_server_leaves_the_desk_surface_untouched(tmp_app, monkeypatch):
    """End to end: boot the real app (lifespan and all) with the Mac registered, and
    assert the machine's capabilities come out the other side unchanged."""
    import importlib

    from server_harness import LiveServer

    transport = SentinelTransport()
    log_sink = lambda _msg: None            # noqa: E731 — identity is what is asserted
    notifier = lambda _msg: None            # noqa: E731
    monkeypatch.setattr(caps, "_profile", "mac", raising=False)
    monkeypatch.setattr(caps, "_audio_transport", transport, raising=False)
    monkeypatch.setattr(caps, "_log_sink", log_sink, raising=False)
    monkeypatch.setattr(caps, "_notifier", notifier, raising=False)
    monkeypatch.setenv("VOICE_AGENT_TOKEN", "test-token-untouched")

    app_module = importlib.import_module("server.app")
    with LiveServer(app_module.app):
        assert caps.profile() == "mac"
        assert caps.audio_transport() is transport
        assert caps._log_sink is log_sink
        assert caps._notifier is notifier

    assert caps.profile() == "mac"
    assert caps.audio_transport() is transport
