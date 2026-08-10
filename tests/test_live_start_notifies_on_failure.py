"""Live-start failures must reach Robin when no session can speak for itself."""
import queue

import pytest


@pytest.fixture
def notifying_agent_module(monkeypatch):
    """Import the menubar agent with notifications captured instead of displayed."""
    from core import caps

    monkeypatch.setattr(caps, "_profile", caps._profile, raising=False)
    monkeypatch.setattr(caps, "_log_sink", caps._log_sink, raising=False)
    monkeypatch.setattr(caps, "_notifier", caps._notifier, raising=False)
    monkeypatch.setattr(caps, "_audio_transport", caps._audio_transport, raising=False)

    import mac.agent as agent

    notifications = []
    monkeypatch.setattr(agent, "LOG", lambda _msg: None)
    monkeypatch.setattr(agent.rumps, "notification",
                        lambda *args, **kwargs: notifications.append((args, kwargs)))
    monkeypatch.setattr(caps, "_notifier", lambda _msg: None, raising=False)
    monkeypatch.setattr(caps, "_log_sink", lambda _msg: None, raising=False)
    return agent, notifications


def desk_app(agent, **overrides):
    """A bare VoiceAgent with the state needed by live-session start paths."""
    app = agent.VoiceAgent.__new__(agent.VoiceAgent)
    app.status = "idle"
    app.live_on = False
    app.live = None
    app._announcing = set()
    app._spoken_tasks = queue.Queue()
    app._term_win_id = None
    for key, value in overrides.items():
        setattr(app, key, value)
    return app


class AlwaysFailingLive:
    attempts = 0

    def __init__(self, *_args, **_kwargs):
        type(self).attempts += 1
        raise RuntimeError("live session unavailable")


class FailsOnceLive:
    attempts = 0

    def __init__(self, *_args, **_kwargs):
        type(self).attempts += 1
        if type(self).attempts == 1:
            raise RuntimeError("transient live-session failure")

    def start(self):
        pass


class CountingLive:
    attempts = 0

    def __init__(self, *_args, **_kwargs):
        type(self).attempts += 1

    def start(self):
        pass


def test_wake_and_speak_retries_once_then_notifies_and_returns_task_text(
        notifying_agent_module, monkeypatch):
    from core import live_session

    agent, notifications = notifying_agent_module
    AlwaysFailingLive.attempts = 0
    monkeypatch.setattr(live_session, "LiveSession", AlwaysFailingLive)
    app = desk_app(agent)

    task_text = "build finished: 3 tests failed"
    assert app._wake_and_speak(task_text) is False
    assert AlwaysFailingLive.attempts == 2
    assert any(task_text in args[2] for args, _kwargs in notifications)


def test_wake_and_speak_recovers_on_the_retry(notifying_agent_module, monkeypatch):
    from core import live_session

    agent, notifications = notifying_agent_module
    FailsOnceLive.attempts = 0
    monkeypatch.setattr(live_session, "LiveSession", FailsOnceLive)
    app = desk_app(agent)

    assert app._wake_and_speak("your build finished") is True
    assert FailsOnceLive.attempts == 2
    assert app.live_on is True
    assert notifications == []


def test_floor_busy_does_not_retry_or_notify(notifying_agent_module, monkeypatch):
    from core import floor, live_session

    agent, notifications = notifying_agent_module
    CountingLive.attempts = 0
    monkeypatch.setattr(live_session, "LiveSession", CountingLive)
    floor.claim(floor.PHONE, "phone:surface", session=object())
    app = desk_app(agent)

    assert app._wake_and_speak("your build finished") is False
    assert CountingLive.attempts == 1
    assert notifications == []


def test_toggle_live_notifies_on_start_failure(notifying_agent_module, monkeypatch):
    from core import live_session

    agent, notifications = notifying_agent_module
    AlwaysFailingLive.attempts = 0
    monkeypatch.setattr(live_session, "LiveSession", AlwaysFailingLive)
    app = desk_app(agent)

    app.toggle_live()

    assert AlwaysFailingLive.attempts == 1
    assert notifications
