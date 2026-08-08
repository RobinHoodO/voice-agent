"""Urgent-only proactive wake: Pam may speak unprompted, but only for priority ≤ 1
attention items (approvals, escalations), once per item, and never as a decision.

Robin explicitly chose "urgent only" over "urgent + fleet failures" (2026-08-08):
run-failures are priority 2 and must NOT wake him — they stay in Telegram/CC per
the 2026-07-17 noise decision.
"""
from kernel_tools import URGENT_PRIORITY_MAX, kernel_urgent, urgent_wake_text


def _att(key, source, priority, title="thing", detail=None):
    return {"key": key, "source": source, "priority": priority,
            "title": title, "detail": detail, "hint": None}


STATUS = {
    "approvals": [{"id": 9, "actionType": "front.send", "preview": "send the reply"}],
    "attention": [
        _att("appr-9", "approvals", 1, "Approval waiting: send the reply to Mingle"),
        _att("esc-1", "escalation", 1, "Worker escalated: budget question"),
        _att("fail-1", "run-failures", 2, "notion-intake failed twice"),
        _att("recv-1", "receivables", 3, "invoice overdue"),
    ],
    "runs": [],
}


def test_only_priority_one_items_qualify():
    keys = [k for k, _ in kernel_urgent(STATUS)]
    assert keys == ["attention:appr-9", "attention:esc-1"]


def test_run_failures_never_wake_robin():
    """Priority 2 by the kernel's own triage — the declined 'fleet failures' tier."""
    assert URGENT_PRIORITY_MAX == 1
    only_failure = {"attention": [_att("fail-1", "run-failures", 2)]}
    assert kernel_urgent(only_failure) == []


def test_raw_approvals_list_is_ignored():
    """The approvals TABLE includes routine items; urgency comes only from the
    attention router's own filing. Using the raw list would recreate the noise
    problem the 2026-07-17 decision solved."""
    routine_only = {"approvals": [{"id": 1, "preview": "routine mirror write"}],
                    "attention": []}
    assert kernel_urgent(routine_only) == []


def test_malformed_rows_are_skipped_not_fatal():
    bad = {"attention": [{"key": None, "priority": 1}, {"key": "x", "priority": "high"},
                         _att("ok", "approvals", 0, "real one")]}
    assert [k for k, _ in kernel_urgent(bad)] == ["attention:ok"]


def test_wake_text_reports_and_asks_but_never_decides():
    items = kernel_urgent(STATUS)
    text = urgent_wake_text(items)
    assert "2 items" in text
    assert "send the reply to Mingle" in text
    assert "confirmation" in text, "acting must still route through the spoken gate"
    for tag in ("VERIFIED", "FAILED"):
        assert tag not in text, "not a task result — no status tag to misread"


def test_wake_text_caps_the_spoken_list():
    items = [(f"attention:k{i}", f"item {i}") for i in range(6)]
    assert "(and 3 more)" in urgent_wake_text(items)


# --- the once-per-item / no-replay gates live in agent.py; pin their contract ------

def test_first_snapshot_seeds_silently_and_second_wakes_once():
    """Replays the gate logic of ThrivbeVoice._apply_urgent_snapshot without AppKit:
    launch-seed is silent, a NEW item wakes exactly once, an already-woken item
    never wakes again."""
    import types

    class Host:
        _apply_urgent_snapshot = None   # filled below
        live_on = False
        _urgent_snapshot = None
        _woken_keys = None
        woke = []

        def _wake_and_speak(self, text, announce_tid=None):
            self.woke.append(text)
            return True

    import agent
    host = Host()
    host._apply_urgent_snapshot = types.MethodType(
        agent.VoiceAgent._apply_urgent_snapshot, host)

    import config
    orig = config.get

    def fake_get(path, default=None):
        if path == "live.proactive_wake":
            return True
        if path == "live.quiet_hours":
            return {}          # quiet hours off
        return orig(path, default)

    config.get, agent_config_get = fake_get, config.get
    try:
        host._urgent_snapshot = [("attention:a", "first")]
        host._apply_urgent_snapshot()
        assert host.woke == [] and host._woken_keys == {"attention:a"}, "launch seed is silent"

        host._urgent_snapshot = [("attention:a", "first"), ("attention:b", "second")]
        host._apply_urgent_snapshot()
        assert len(host.woke) == 1 and "second" in host.woke[0]
        assert host._woken_keys == {"attention:a", "attention:b"}

        host._urgent_snapshot = [("attention:b", "second")]
        host._apply_urgent_snapshot()
        assert len(host.woke) == 1, "an already-woken item must never re-wake"
    finally:
        config.get = agent_config_get


def test_live_session_defers_the_wake_for_a_later_tick():
    import types

    class Host:
        live_on = True
        _urgent_snapshot = None
        _woken_keys = set()
        woke = []

        def _wake_and_speak(self, text, announce_tid=None):
            self.woke.append(text)
            return True

    import agent
    import config
    host = Host()
    host._apply_urgent_snapshot = types.MethodType(
        agent.VoiceAgent._apply_urgent_snapshot, host)
    orig = config.get
    config.get = lambda p, d=None: True if p == "live.proactive_wake" else orig(p, d)
    try:
        host._urgent_snapshot = [("attention:x", "urgent thing")]
        host._apply_urgent_snapshot()
        assert host.woke == []
        assert "attention:x" not in host._woken_keys, "not marked — must retry when idle"
    finally:
        config.get = orig
