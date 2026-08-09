"""Turn detection must wait for Robin to actually finish talking.

Regression guard for the bug where the agent acted mid-sentence: a 700ms silence
timer treated every thinking pause as end-of-turn.
"""
from core import config
from core.backends.openai_backend import OpenAIBackend, _turn_detection_cfg


def _td(setup: dict) -> dict:
    return setup["session"]["audio"]["input"]["turn_detection"]


def test_defaults_to_semantic_vad_at_lowest_eagerness(monkeypatch):
    monkeypatch.setattr(config, "get", lambda k: None)
    cfg = _turn_detection_cfg()
    assert cfg["type"] == "semantic_vad"
    assert cfg["eagerness"] == "low"


def test_server_mode_timer_is_well_clear_of_a_thinking_pause(monkeypatch):
    monkeypatch.setattr(config, "get", lambda k: {"mode": "server"} if k == "live.turn_detection" else None)
    cfg = _turn_detection_cfg()
    assert cfg["type"] == "server_vad"
    # the old value was 700ms, which fired on ordinary mid-sentence pauses
    assert cfg["silence_duration_ms"] >= 1200


def test_overrides_are_honoured(monkeypatch):
    monkeypatch.setattr(config, "get", lambda k: {
        "mode": "server", "silence_ms": 2500, "threshold": 0.8} if k == "live.turn_detection" else None)
    cfg = _turn_detection_cfg()
    assert cfg["silence_duration_ms"] == 2500 and cfg["threshold"] == 0.8


def test_create_response_stays_false_in_both_modes(monkeypatch):
    """The app injects cursor context then triggers the response itself — if the
    API auto-responded, that context would be missing from every turn."""
    for mode in ("semantic", "server"):
        monkeypatch.setattr(config, "get",
                            lambda k, m=mode: {"mode": m} if k == "live.turn_detection" else None)
        assert _turn_detection_cfg()["create_response"] is False


def test_setup_payload_carries_it(monkeypatch):
    monkeypatch.setattr(config, "get", lambda k: None)
    assert _td(OpenAIBackend().build_setup("instr", [], "alloy"))["type"] == "semantic_vad"
