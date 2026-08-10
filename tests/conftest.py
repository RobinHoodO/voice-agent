"""Shared test fixtures.

The app stores config + the conversation DB under ~/Library/Application Support.
Tests must never touch the real ones, so `tmp_app` redirects every config path
(and memory's module-global DB_PATH) at a throwaway tmp dir per test.

`voice_server` builds on it for the server surface: a real uvicorn running
`server.app:app`, real LiveSessions, a fake provider socket.
"""
import importlib
import sys

import pytest


@pytest.fixture(autouse=True)
def isolated_action_journal(monkeypatch, tmp_path_factory):
    """No test ever appends to Robin's real `actions.jsonl`.

    Autouse and unconditional: the journal is written from `core.live_session._do_tool`,
    so ANY test that dispatches a tool would otherwise leave a line in the accountability
    record of a machine nobody was operating. An audit trail with test noise in it is
    not an audit trail.
    """
    target = tmp_path_factory.mktemp("audit") / "actions.jsonl"
    monkeypatch.setenv("VOICE_AGENT_ACTIONS_LOG", str(target))
    return target


@pytest.fixture(autouse=True)
def isolated_agent_log(monkeypatch, tmp_path_factory):
    """No test ever appends to Robin's real `agent.log`.

    Same argument as the action journal above, and it bit for real on 2026-08-10: a suite
    run left `live $ rm -rf /tmp/whatever`, `env rm -rf /opt` and
    `gmail_send ... -> sent` (with plausible-looking addresses) in the live log while Robin
    was reading it to diagnose why Pam had gone mute. Nothing had been deleted and no mail
    was sent — but a diagnostic log you have to mentally filter is worse than no log.

    `mac.agent` copies LOG_PATH into a module global at import, so patching `core.config`
    alone is not enough; both are redirected, and `mac.agent` is only touched if it is
    already imported (it needs PyObjC, which not every test environment has).
    """
    target = tmp_path_factory.mktemp("logs")
    monkeypatch.setattr("core.config.LOG_PATH", str(target / "agent.log"), raising=False)
    monkeypatch.setattr("core.config.ACTIVITY_PATH", str(target / "activity.log"), raising=False)
    agent_mod = sys.modules.get("mac.agent")
    if agent_mod is not None:
        monkeypatch.setattr(agent_mod, "LOG_PATH", str(target / "agent.log"), raising=False)
    return target


@pytest.fixture(autouse=True)
def clean_conversation_floor():
    """No test starts holding the conversation floor, and none leaks it to the next.

    `core.floor` is a process global on purpose — there is one brain in this process —
    which makes it exactly the kind of state that turns one failing test into five.
    Reaching at `_holder` rather than adding a `reset()` is deliberate: a production
    API whose only caller is a fixture is a worse thing to own than this line.
    """
    from core import floor

    floor.floor()._holder = None
    yield
    floor.floor()._holder = None


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

    # Setting the env var is NOT enough: `secrets.secret` consults the registered store
    # (the macOS Keychain) FIRST and only falls back to env. The moment Robin turned the
    # phone surface on, `mac/phone_surface.ensure_token()` minted a real token into the
    # Keychain — which then beat this one and made every server test fail on a 401 that
    # looked like "the session never said hello". The suite must not depend on whether a
    # feature is switched on, so resolve this one key here and let everything else through.
    from core import secrets as _secrets

    _real_secret = _secrets.secret

    def _test_secret(name, env_fallback=()):
        if name == "voice_agent_token":
            return VOICE_TEST_TOKEN
        return _real_secret(name, env_fallback)

    monkeypatch.setattr(_secrets, "secret", _test_secret)
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
