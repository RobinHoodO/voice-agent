"""Auto-wake-and-speak must go quiet overnight (the 3am incident) but only on
the unattended announce path — see agent._in_quiet_hours / agent._check_tasks."""
import time

import config
from agent import _in_quiet_hours


def _at(weekday, hour, minute):
    # struct_time fields: (year, mon, mday, hour, min, sec, wday, yday, isdst)
    return time.struct_time((2026, 8, 3, hour, minute, 0, weekday, 1, -1))


def _stub_quiet_hours(monkeypatch, **overrides):
    qh = {"enabled": True, "start": "00:00", "end": "07:00", "weekdays_only": True}
    qh.update(overrides)
    monkeypatch.setattr(config, "get",
                         lambda path, default=None: qh if path == "live.quiet_hours" else default)


def test_default_config_is_weekday_0007(tmp_app):
    qh = config.get("live.quiet_hours")
    assert qh["enabled"] is True
    assert qh["start"] == "00:00" and qh["end"] == "07:00"
    assert qh["weekdays_only"] is True


def test_silences_3am_weekday(monkeypatch):
    _stub_quiet_hours(monkeypatch)
    monkeypatch.setattr(time, "localtime", lambda: _at(0, 3, 0))   # Monday 3am
    assert _in_quiet_hours() is True


def test_allows_weekend_3am_when_weekdays_only(monkeypatch):
    _stub_quiet_hours(monkeypatch)
    monkeypatch.setattr(time, "localtime", lambda: _at(5, 3, 0))   # Saturday 3am
    assert _in_quiet_hours() is False


def test_allows_daytime_weekday(monkeypatch):
    _stub_quiet_hours(monkeypatch)
    monkeypatch.setattr(time, "localtime", lambda: _at(2, 14, 0))  # Wednesday 2pm
    assert _in_quiet_hours() is False


def test_disabled_never_silences(monkeypatch):
    _stub_quiet_hours(monkeypatch, enabled=False)
    monkeypatch.setattr(time, "localtime", lambda: _at(0, 3, 0))
    assert _in_quiet_hours() is False


def test_wraparound_window(monkeypatch):
    _stub_quiet_hours(monkeypatch, start="22:00", end="06:00", weekdays_only=False)
    monkeypatch.setattr(time, "localtime", lambda: _at(0, 23, 30))  # 11:30pm
    assert _in_quiet_hours() is True
    monkeypatch.setattr(time, "localtime", lambda: _at(0, 12, 0))   # noon
    assert _in_quiet_hours() is False
