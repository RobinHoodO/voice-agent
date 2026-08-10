"""The menubar half: seeing that the phone surface is on, turning it off, and the floor.

`mac/agent.py` is the surface Robin actually uses, and none of it was covered — the
conversation floor is claimed there, the phone switch lives there, and a finished
background task is routed there. Constructing a real `VoiceAgent` would start an
AppKit run loop, a keyboard listener and four timers, so these drive the methods on an
uninitialised instance with exactly the attributes each one touches. That is the same
trick `test_phone_does_not_steal_the_mac.py` uses on `LiveSession`, and it is honest:
the code under test is the shipped code, not a copy.
"""
import os
import queue

import pytest


@pytest.fixture
def agent_module(monkeypatch):
    """Import `mac.agent` without letting it keep this process.

    Importing it runs `caps_install.install(log_sink=LOG)` at module level, which
    registers the Mac's log sink (pointed at Robin's real `agent.log`), its notifier
    (a real desktop notification) and PortAudio. Snapshot before, restore after.
    """
    from core import caps

    monkeypatch.setattr(caps, "_profile", caps._profile, raising=False)
    monkeypatch.setattr(caps, "_log_sink", caps._log_sink, raising=False)
    monkeypatch.setattr(caps, "_notifier", caps._notifier, raising=False)
    monkeypatch.setattr(caps, "_audio_transport", caps._audio_transport, raising=False)

    import mac.agent as agent

    monkeypatch.setattr(agent, "LOG", lambda _msg: None)
    monkeypatch.setattr(agent.rumps, "notification", lambda *a, **k: None)
    monkeypatch.setattr(caps, "_notifier", lambda _msg: None, raising=False)
    monkeypatch.setattr(caps, "_log_sink", lambda _msg: None, raising=False)
    return agent


class Item:
    """A stand-in for rumps.MenuItem: the menu title is all these tests read."""

    def __init__(self, title=""):
        self.title = title


class FakeSurface:
    def __init__(self, status):
        self._status = dict(status)
        self.toggles = 0

    def status(self):
        return dict(self._status)

    def toggle(self):
        self.toggles += 1
        self._status["on"] = not self._status["on"]
        return self.status()


class FakeLive:
    """A stand-in for LiveSession: records stop(), and can accept an offered task."""

    def __init__(self, *_a, **_k):
        self.stopped = 0
        self.started = 0
        self.offered = []

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1

    def offer_task(self, tid, text):
        self.offered.append((tid, text))
        return True


def desk_app(agent, **overrides):
    """A VoiceAgent with just enough state for the methods under test."""
    app = agent.VoiceAgent.__new__(agent.VoiceAgent)
    app.status = "idle"
    app.live_on = False
    app.live = None
    app.phone = None
    app._phone_item = Item()
    app._phone_link_item = Item()
    app._announcing = set()
    app._announced = set()
    app._spoken_tasks = queue.Queue()
    app._term_win_id = None
    for k, v in overrides.items():
        setattr(app, k, v)
    return app


# ── requirement: see that it is on, and turn it off ──────────────────────────
@pytest.mark.parametrize("status,needle", [
    ({"on": False}, "off"),
    ({"on": True, "tls_port": 8443, "in_conversation": False}, "on · 8443"),
    ({"on": True, "tls_port": 8443, "in_conversation": True}, "in conversation"),
    ({"on": False, "error": "Tailscale is not up on this Mac."}, "Tailscale is not up"),
])
def test_the_menu_title_says_what_the_surface_is_doing(agent_module, status, needle):
    app = desk_app(agent_module, phone=FakeSurface(status))
    app._reconcile_phone_menu()
    assert needle in app._phone_item.title


def test_the_menu_item_turns_the_surface_off(agent_module):
    surface = FakeSurface({"on": True, "tls_port": 8443, "url": "https://x.ts.net:8443/"})
    app = desk_app(agent_module, phone=surface)

    app._toggle_phone()

    assert surface.toggles == 1
    assert surface.status()["on"] is False
    assert "off" in app._phone_item.title


def test_the_menu_never_shows_the_token(agent_module):
    """The menu title is visible over Robin's shoulder in every screen share. The URL
    belongs there; the shared secret does not — it goes to the clipboard on request."""
    surface = FakeSurface({"on": True, "tls_port": 8443,
                           "url": "https://x.ts.net:8443/", "in_conversation": False})
    app = desk_app(agent_module, phone=surface)
    app._reconcile_phone_menu()
    assert "token" not in app._phone_item.title.lower()


def test_the_surface_is_not_built_until_it_is_used(agent_module):
    """Importing FastAPI/uvicorn at launch is a cost the app should not pay while the
    surface is off, which is the default."""
    app = desk_app(agent_module)
    assert app.phone is None
    app._reconcile_phone_menu()
    assert app.phone is None, "reconciling the menu built the whole HTTP stack"


def test_a_disabled_surface_is_not_started_at_launch(agent_module, tmp_app):
    from core import config

    config.set_("phone_surface.enabled", False)
    app = desk_app(agent_module)
    app._start_phone_if_enabled()
    assert app.phone is None


# ── the desk side of the floor ───────────────────────────────────────────────
def test_a_double_tap_takes_the_floor_and_stopping_gives_it_back(agent_module, monkeypatch):
    """Double-tapping Control is Robin, at this keyboard, deliberately — the one act
    the floor lets end another conversation."""
    from core import floor, live_session

    monkeypatch.setattr(live_session, "LiveSession", FakeLive)
    phone_session = FakeLive()
    evicted = []
    floor.claim(floor.PHONE, "phone:surface", session=phone_session,
                on_evict=lambda: evicted.append(True) or phone_session.stop())

    app = desk_app(agent_module)
    app.toggle_live()

    assert floor.holder().surface == floor.DESK
    assert evicted == [True], "the desk did not evict the phone"
    assert phone_session.stopped == 1, "the phone session was discarded, not stopped"
    assert app.live_on is True

    app.toggle_live()
    assert floor.holder() is None, "ending the desk conversation kept the floor"


def test_an_auto_wake_is_refused_rather_than_barging_in(agent_module, monkeypatch):
    """A finished background task is the agent's own idea, not Robin's. It must never
    end the conversation he is having on his phone."""
    from core import floor, live_session

    monkeypatch.setattr(live_session, "LiveSession", FakeLive)
    phone_session = FakeLive()
    evicted = []
    floor.claim(floor.PHONE, "phone:surface", session=phone_session,
                on_evict=lambda: evicted.append(True))

    app = desk_app(agent_module)
    assert app._wake_and_speak("your build finished") is False
    assert evicted == [], "an auto-wake evicted the phone"
    assert phone_session.stopped == 0
    assert app.live_on is False
    assert floor.holder().surface == floor.PHONE


def test_an_auto_wake_on_a_free_floor_still_works(agent_module, monkeypatch):
    """The refusal above must not have broken the ordinary case."""
    from core import floor, live_session

    monkeypatch.setattr(live_session, "LiveSession", FakeLive)
    app = desk_app(agent_module)
    assert app._wake_and_speak("your build finished") is True
    assert app.live_on is True
    assert floor.holder().surface == floor.DESK


def test_being_evicted_ends_the_desk_session_through_its_own_stop(agent_module):
    """What the phone's "Take over" button ultimately triggers here."""
    from core import floor

    live = FakeLive()
    app = desk_app(agent_module, live=live, live_on=True, status="listening")
    floor.claim(floor.DESK, app.FLOOR_KEY, session=live,
                on_evict=app._evicted_from_floor)

    app._evicted_from_floor()

    assert live.stopped == 1
    assert app.live_on is False
    assert app.live is None
    assert app.status == "idle"


# ── a finished job follows Robin to whichever seat he is in ──────────────────
def test_a_finished_task_is_offered_to_the_phone_when_he_is_on_it(agent_module, tmp_app):
    """One brain: a job finishing while Robin is on the sofa belongs in the conversation
    he is actually in, not spoken at an empty room from the Mac's speaker."""
    from core import config, floor

    config.ensure_dirs()
    for name, body in (("job1.out", "the deploy finished"), ("job1.done", "")):
        with open(os.path.join(config.TASKS_DIR, name), "w", encoding="utf-8") as f:
            f.write(body)

    phone_session = FakeLive()
    floor.claim(floor.PHONE, "phone:surface", session=phone_session)

    app = desk_app(agent_module)
    app._check_tasks(None)

    assert phone_session.offered == [("job1", "the deploy finished")]


def test_a_finished_task_still_wakes_the_desk_when_nobody_is_talking(agent_module,
                                                                    tmp_app, monkeypatch):
    from core import config, live_session

    monkeypatch.setattr(live_session, "LiveSession", FakeLive)
    monkeypatch.setattr(agent_module, "_in_quiet_hours", lambda: False)
    config.ensure_dirs()
    for name, body in (("job2.out", "done"), ("job2.done", "")):
        with open(os.path.join(config.TASKS_DIR, name), "w", encoding="utf-8") as f:
            f.write(body)

    app = desk_app(agent_module)
    app._check_tasks(None)

    assert app.live_on is True, "an idle Mac stopped announcing finished jobs"
