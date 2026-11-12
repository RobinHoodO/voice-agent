"""Pam's hands: which actions run at once, and which wait for a spoken yes."""
import pytest

from core import caps, hands


class FakeHands(caps.Hands):
    def __init__(self, under=""):
        self.under, self.calls = under, []

    def click(self, label=None, x=None, y=None, double=False):
        self.calls.append(("click", label, x, y, double))
        return "clicked"

    def element_at(self, x, y):
        return self.under

    def press_keys(self, keys):
        self.calls.append(("keys", keys))
        return "pressed"


@pytest.fixture
def fake(monkeypatch):
    f = FakeHands()
    monkeypatch.setattr(caps, "_hands", f)
    return f


@pytest.mark.parametrize("label", ["Send", "Delete Message", "Pay now", "Publiser", "Post"])
def test_outward_or_destructive_labels_need_a_spoken_yes(fake, label):
    assert hands.needs_confirmation("click", {"label": label})


@pytest.mark.parametrize("label", ["New Message", "Search", "Reply", "Sign in", "Posts"])
def test_ordinary_clicks_run_at_once(fake, label):
    assert hands.needs_confirmation("click", {"label": label}) == ""


def test_a_blind_xy_click_is_judged_by_what_is_under_it(fake):
    fake.under = "Send"
    assert hands.needs_confirmation("click", {"x": 500, "y": 900})
    fake.under = "Subject"
    assert hands.needs_confirmation("click", {"x": 500, "y": 900}) == ""


def test_send_shortcut_needs_a_yes_however_it_is_spelled(fake):
    assert hands.normalize_keys("Command + Return") == "cmd+return"
    assert hands.needs_confirmation("press_keys", {"keys": "Command + Return"})
    assert hands.needs_confirmation("press_keys", {"keys": "cmd+t"}) == ""


def test_run_passes_normalized_args_through(fake):
    hands.run("click", {"x": "10", "y": 20})
    hands.run("press_keys", {"keys": "CMD+L"})
    assert fake.calls == [("click", None, 10.0, 20.0, False), ("keys", "cmd+l")]


@pytest.mark.parametrize("target", ["/tmp/x.command", "~/evil.app", "./run.sh", "file:///tmp/x"])
def test_open_app_never_opens_a_path(target):
    """`open x.command` RUNS it — open_app takes an app name or an http(s) URL only."""
    from mac.hands import MacHands
    assert "not a file path" in MacHands().open_app(target)
