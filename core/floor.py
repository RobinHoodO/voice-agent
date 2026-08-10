"""The conversation floor: one brain, one SEAT at a time.

Robin ruled on 2026-08-10 that Mac-Pam IS the system — one brain, on his Mac. The phone
is a remote microphone and speaker for that same process, not a second agent. That makes
a collision REAL rather than hypothetical: he is mid-sentence at his desk and his phone,
in his pocket, reconnects its PWA.

Two seats at once is not the answer. They would share this Mac's memory store, this
Mac's shell, this Mac's kernel — and, in the same room, this Mac's speaker leaking into
the phone's microphone, which the barge-in detector reads as Robin interrupting. So
there is ONE floor and this module holds it.

WHAT THE FLOOR IS AND IS NOT SCOPED TO
    A SEAT — the desk, or the phone — not a socket. The phone surface holds one claim
    however many tabs it has open, and inside that claim the per-tab isolation that was
    already there still governs: one `LiveSession` per tab, so the one staged
    high-stakes action lives on that tab's own object and a spoken "yes" in another tab
    cannot reach it (`tests/test_server_auth.py::test_two_tabs_never_cross_confirm`).
    Those are different questions and they get different mechanisms. This one is about
    Robin being in two PLACES; that one is about a confirmation being bound to the
    sentence he was actually read.

THE RULE, and the reason it is this one:

    A live conversation is never ended by a CONNECTION. It is ended by Robin, or by the
    idle/max watchdogs core already runs. A claimant that arrives while someone holds
    the floor is REFUSED and told who has it.

    A deliberate human act may still take the floor — the phone's "Take over" button,
    the desk's double-tap — and a takeover ends the other conversation through the same
    `stop()` a spoken sign-off uses, so its transcript is persisted by the normal path.

Why this one cannot lose a turn: the set of things that can end a conversation is
exactly {Robin, the watchdogs}. An iOS PWA restoring itself, a tailnet flap, a
background tab waking, a service worker reviving a socket — none of them carry a human
decision, so none of them can take the floor. Automatic takeover fails precisely here:
it is indistinguishable, at the server, from Robin deliberately moving to the sofa, and
the cost of guessing wrong is his half-finished sentence at the desk.

`take()` is deliberately blunt once it IS called: it evicts, waits briefly for the loser
to wind down, and claims regardless. The floor is the RIGHT to speak, not the other
thread's lifetime — a session whose audio thread is slow to exit must not be able to
lock Robin out of his own agent.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

# The two seats. Same strings as the capability profiles in `core.capabilities` for
# PHONE; DESK is spelled out rather than reusing "mac" because this names WHERE ROBIN
# IS, and every profile now acts on the same Mac.
DESK = "desk"
PHONE = "phone"


@dataclass(frozen=True)
class Holder:
    """Who holds the floor. Immutable — a change of holder is a new object."""

    surface: str
    key: str
    since: float
    session: object | None = None
    on_evict: object | None = None      # callable(): ask this holder to end, politely

    def age_s(self, now: float | None = None) -> float:
        return max(0.0, (now if now is not None else time.monotonic()) - self.since)

    def describe(self) -> str:
        """One sentence for the surface that was just refused. Robin reads this."""
        seat = "at your desk" if self.surface == DESK else f"on your {self.surface}"
        return (f"Pam is already in a conversation {seat} "
                f"({int(self.age_s())}s in). One brain — it has to finish, or you can "
                f"take over.")


class Busy(Exception):
    """The floor is held by someone else. Carries them, so the caller can say who."""

    def __init__(self, holder: Holder) -> None:
        super().__init__(holder.describe())
        self.holder = holder


class Floor:
    """The single floor. Thread-safe: claimed from the AppKit thread (desk hotkey) and
    from the server's event loop (phone), released from session threads."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._holder: Holder | None = None

    # --- reading ------------------------------------------------------------
    def holder(self) -> Holder | None:
        with self._lock:
            return self._holder

    def busy(self) -> bool:
        return self.holder() is not None

    def held_by(self, surface: str) -> bool:
        h = self.holder()
        return h is not None and h.surface == surface

    def session(self):
        """The LiveSession that currently holds the floor, if it registered one.

        This is what lets a finished background task reach whoever Robin is actually
        talking to instead of the surface that happened to launch it.
        """
        h = self.holder()
        return h.session if h is not None else None

    # --- taking -------------------------------------------------------------
    def claim(self, surface: str, key: str, session=None, on_evict=None) -> Holder:
        """Take the floor, or raise `Busy`. Never disturbs a live conversation.

        Re-claiming with the SAME key succeeds and refreshes the holder: that is the
        same SEAT arriving again — a tab reconnecting after a dropped mobile network,
        or a second tab on the same phone — not a rival claimant.
        """
        with self._lock:
            current = self._holder
            if current is not None and current.key != key:
                raise Busy(current)
            return self._install(surface, key, session, on_evict)

    def take(self, surface: str, key: str, session=None, on_evict=None,
             timeout: float = 6.0) -> Holder:
        """Take the floor even if it is held. ONLY for a deliberate human act.

        The loser is asked to stop through its own `on_evict` — which runs the normal
        `LiveSession.stop()` path, so its transcript is persisted exactly as a spoken
        sign-off would persist it. Nothing here is a kill.
        """
        with self._lock:
            current = self._holder
            if current is None or current.key == key:
                return self._install(surface, key, session, on_evict)
            evict = current.on_evict
        if evict is not None:
            try:
                evict()
            except Exception:
                pass          # a holder that cannot stop itself still loses the floor
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                held = self._holder
                if held is None or held.key != current.key:
                    break
            time.sleep(0.05)
        with self._lock:
            return self._install(surface, key, session, on_evict)

    def release(self, key: str) -> bool:
        """Give up the floor. A no-op unless `key` is the holder.

        The key check is the whole point: an evicted session releases from its own
        thread AFTER the winner has already claimed, and a blind clear there would hand
        the floor back to nobody while the new conversation is running.
        """
        with self._lock:
            if self._holder is not None and self._holder.key == key:
                self._holder = None
                return True
            return False

    def _install(self, surface: str, key: str, session, on_evict) -> Holder:
        holder = Holder(surface=surface, key=key, since=time.monotonic(),
                        session=session, on_evict=on_evict)
        self._holder = holder
        return holder


# The process's one floor. A module global for the same reason `core.caps` is: there is
# one brain in this process, so there is one of these.
_FLOOR = Floor()


def floor() -> Floor:
    return _FLOOR


# Convenience passthroughs — the surfaces read better for them.
def holder() -> Holder | None:
    return _FLOOR.holder()


def busy() -> bool:
    return _FLOOR.busy()


def session():
    return _FLOOR.session()


def claim(surface: str, key: str, session=None, on_evict=None) -> Holder:
    return _FLOOR.claim(surface, key, session, on_evict)


def take(surface: str, key: str, session=None, on_evict=None, timeout: float = 6.0) -> Holder:
    return _FLOOR.take(surface, key, session, on_evict, timeout)


def release(key: str) -> bool:
    return _FLOOR.release(key)
