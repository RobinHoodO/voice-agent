"""The floor: one brain, one seat, and no way for a reconnect to end a conversation.

These are the unit-level guarantees `core.floor` exists to make. The one that matters
most is negative and easy to lose in a refactor: a claim NEVER stops anybody. If
`claim` ever grows an "it had been idle a while, so…" branch, the phone waking up in
Robin's pocket starts ending his sentences at the desk again — so the test asserts on
the eviction callback not being called, not merely on the return value.
"""
import time

import pytest

from core import floor as floor_module
from core.floor import DESK, PHONE, Busy, Floor


class FakeSession:
    """Stands in for a LiveSession: it only has to record that it was asked to stop."""

    def __init__(self, name="s"):
        self.name = name
        self.stopped = 0

    def stop(self):
        self.stopped += 1


def desk(f, session=None, key="desk:menubar"):
    """Claim the floor as the desk, the way mac/agent.py does."""
    session = session or FakeSession("desk")
    evicted = []

    def on_evict():
        evicted.append(True)
        session.stop()

    f.claim(DESK, key, session=session, on_evict=on_evict)
    return session, evicted


# ── the rule: a connection never ends a conversation ─────────────────────────
def test_a_second_seat_is_refused_and_the_first_is_left_alone():
    f = Floor()
    session, evicted = desk(f)

    with pytest.raises(Busy) as raised:
        f.claim(PHONE, "phone:surface")

    assert raised.value.holder.surface == DESK
    assert session.stopped == 0, "claiming the floor stopped the conversation on it"
    assert evicted == [], "a claim asked the holder to leave — claims must never evict"
    assert f.holder().surface == DESK


def test_the_refusal_says_who_has_it_and_for_how_long():
    """Robin reads this sentence on his phone; it has to name the other seat."""
    f = Floor()
    desk(f)
    with pytest.raises(Busy) as raised:
        f.claim(PHONE, "phone:surface")
    text = raised.value.holder.describe()
    assert "desk" in text
    assert raised.value.holder.age_s() >= 0


def test_the_same_seat_arriving_again_is_not_a_rival():
    """Two tabs on one phone, or one tab reconnecting: same key, same seat, no refusal.

    This is what keeps the floor from colliding with per-tab session isolation — the
    phone holds ONE claim however many tabs it has, and the staged-action gate stays a
    per-tab affair (tests/test_server_auth.py::test_two_tabs_never_cross_confirm).
    """
    f = Floor()
    first = FakeSession("tab-1")
    second = FakeSession("tab-2")
    f.claim(PHONE, "phone:surface", session=first)
    f.claim(PHONE, "phone:surface", session=second)      # must not raise
    assert first.stopped == 0
    assert f.session() is second, "the newest tab should be the one offered task results"


# ── the exception: a deliberate human act ────────────────────────────────────
def test_take_evicts_through_the_holders_own_stop():
    """A takeover ends the other conversation the way a spoken sign-off does — by
    calling ITS stop(), which is the path that persists the transcript."""
    f = Floor()
    session, evicted = desk(f)

    f.take(PHONE, "phone:surface", session=FakeSession("phone"))

    assert evicted == [True]
    assert session.stopped == 1
    assert f.holder().surface == PHONE


def test_take_on_a_free_floor_is_just_a_claim():
    f = Floor()
    holder = f.take(PHONE, "phone:surface")
    assert holder.surface == PHONE


def test_take_cannot_be_locked_out_by_a_holder_that_will_not_leave():
    """A session whose thread is wedged must not be able to lock Robin out of his own
    agent. The floor is the RIGHT to speak, not the other thread's lifetime."""
    f = Floor()
    f.claim(DESK, "desk:menubar", on_evict=lambda: None)     # never releases

    started = time.monotonic()
    f.take(PHONE, "phone:surface", timeout=0.3)
    elapsed = time.monotonic() - started

    assert f.holder().surface == PHONE
    assert elapsed < 3.0, "take() waited far longer than its timeout"


def test_take_survives_an_eviction_callback_that_raises():
    f = Floor()

    def boom():
        raise RuntimeError("this holder cannot stop itself")

    f.claim(DESK, "desk:menubar", on_evict=boom)
    f.take(PHONE, "phone:surface", timeout=0.2)
    assert f.holder().surface == PHONE


# ── release is keyed, and that is what makes the ordering safe ───────────────
def test_an_evicted_holder_releasing_late_does_not_clear_the_new_holder():
    """The real sequence: the phone takes the floor, and only THEN does the desk
    session's thread reach its own teardown and call release. A blind clear there
    would hand the floor to nobody while the phone is mid-conversation."""
    f = Floor()
    desk(f)
    f.take(PHONE, "phone:surface")

    assert f.release("desk:menubar") is False
    assert f.holder() is not None and f.holder().surface == PHONE


def test_release_by_the_holder_frees_it():
    f = Floor()
    f.claim(PHONE, "phone:surface")
    assert f.release("phone:surface") is True
    assert f.holder() is None
    assert f.busy() is False


def test_release_of_an_empty_floor_is_a_no_op():
    assert Floor().release("nobody") is False


# ── the module-level floor is the one the surfaces share ─────────────────────
def test_the_process_has_exactly_one_floor():
    """The desk claims from mac/agent.py and the phone from server/app.py; if these
    resolved to different objects, neither would ever see the other."""
    assert floor_module.floor() is floor_module.floor()
    floor_module.claim(DESK, "desk:menubar")
    assert floor_module.holder().surface == DESK
    assert floor_module.busy() is True
    floor_module.release("desk:menubar")
    assert floor_module.holder() is None


def test_the_seat_names_match_the_capability_profile():
    """`PHONE` is also the capability profile the browser session pins, and a drift
    between the two would make `floor.held_by(PHONE)` quietly always false."""
    from core import capabilities
    from server.session import SURFACE_PROFILE

    assert PHONE == SURFACE_PROFILE
    assert PHONE in capabilities.PROFILES
