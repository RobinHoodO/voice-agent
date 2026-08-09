"""Shared test fixtures.

The app stores config + the conversation DB under ~/Library/Application Support.
Tests must never touch the real ones, so `tmp_app` redirects every config path
(and memory's module-global DB_PATH) at a throwaway tmp dir per test.

`voice_server` builds on it for the server surface: a real uvicorn running
`server.app:app`, real LiveSessions, a fake provider socket.
"""
import importlib

import pytest


@pytest.fixture
def tmp_app(monkeypatch, tmp_path):
    """Point config + memory storage at an isolated tmp dir. Yields the dir."""
    from core import config

    support = tmp_path / "support"
    logs = tmp_path / "logs"
    monkeypatch.setattr(config, "SUPPORT_DIR", str(support))
    monkeypatch.setattr(config, "LOG_DIR", str(logs))
    monkeypatch.setattr(config, "CONFIG_PATH", str(support / "config.json"))
    monkeypatch.setattr(config, "MEMORY_PATH", str(support / "memory.log"))
    monkeypatch.setattr(config, "LOG_PATH", str(logs / "agent.log"))
    monkeypatch.setattr(config, "ACTIVITY_PATH", str(logs / "activity.log"))
    monkeypatch.setattr(config, "TASKS_DIR", str(support / "tasks"))
    monkeypatch.setattr(config, "_cache", None)   # don't leak a cached config between tests

    # memory.DB_PATH is captured at import from config.SUPPORT_DIR — re-point it.
    memory = importlib.import_module("core.memory")
    monkeypatch.setattr(memory, "DB_PATH", str(support / "conversations.db"))

    return support


VOICE_TEST_TOKEN = "test-token-6f2c1a"


@pytest.fixture
def voice_server(tmp_app, monkeypatch):
    """A live `server.app:app` on an ephemeral port, wired to a fake provider.

    Everything from the socket down to `LiveSession` is the real thing; only the
    provider websocket is faked, and only the three side-effects a unit test must not
    have (the kernel manifest HTTP call, the `pi` learning subprocess, the per-session
    zsh) are stubbed. Yields `(server, sessions)` where `sessions` fills with the real
    `BrowserLiveSession` objects as tabs connect.
    """
    from core import caps, kernel_tools, live_session

    from server_harness import LiveServer, make_recording_factory

    monkeypatch.setenv("VOICE_AGENT_TOKEN", VOICE_TEST_TOKEN)

    # No network: the high-stakes manifest, the cost log, and the prompt builder all
    # reach Thrivbe-1 or the local memory store on a real run.
    monkeypatch.setattr(kernel_tools, "kernel_high_stakes", lambda: ["gmail_send"])
    monkeypatch.setattr(kernel_tools, "session_log", lambda *a, **k: None)
    monkeypatch.setattr(live_session, "_build_live_instructions",
                        lambda ctx, cfg=None, **kwargs: "test instructions")

    class _NoShell:
        def __init__(self, *a, **k):
            """Same signature freedom as the real Shell, which now takes the surface's
            capability profile so it can pick zsh on the Mac and bash on Thrivbe-1."""

        def run(self, *a, **k):
            return ""

        def close(self):
            pass

    monkeypatch.setattr(live_session, "Shell", _NoShell)

    # server.app's lifespan rewires core.caps process-wide; put it back afterwards so
    # the rest of the suite sees the transport it expects. `_profile` is in that list
    # for a reason a leaked value made obvious: it decides which shell binary
    # `core.shell` spawns, so a server profile surviving this fixture had the Mac's own
    # shell test spawning bash.
    monkeypatch.setattr(caps, "_log_sink", caps._log_sink, raising=False)
    monkeypatch.setattr(caps, "_notifier", caps._notifier, raising=False)
    monkeypatch.setattr(caps, "_audio_transport", caps._audio_transport, raising=False)
    monkeypatch.setattr(caps, "_profile", caps._profile, raising=False)

    app_module = importlib.import_module("server.app")
    sessions: list = []
    monkeypatch.setattr(app_module, "SESSION_FACTORY", make_recording_factory(sessions))
    app_module.SESSIONS.clear()

    with LiveServer(app_module.app) as server:
        yield server, sessions
    app_module.SESSIONS.clear()
