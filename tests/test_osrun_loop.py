"""os_delegate closed loop: a finished kernel run must become a spoken sentinel.

Before 2026-08-08 the runId from POST /delegate was discarded — the OS path was
the only delegation path that never reported back by voice (gap G1). The loop
closes by converting kernel run state into the same .out/.done sentinel herdr
delegates use, so the announcer path applies unchanged.
"""
import json
import time

import kernel_tools
from kernel_tools import OSRUN_WINDOW_GRACE_S, osrun_outcome

SIDECAR = {"runId": 42, "instruction": "chase the Mingle invoice", "started": 1000.0}


def _run(status, report=None, error=None):
    return {"id": 42, "process_name": "os-worker", "status": status,
            "started_at": "x", "finished_at": "y", "report": report, "error": error}


def test_running_run_is_left_alone():
    assert osrun_outcome(SIDECAR, [_run("running")], now=2000.0) is None


def test_completed_with_report_is_verified_tag_last():
    out = osrun_outcome(SIDECAR, [_run("completed", report="Invoice chased, reply drafted.")],
                        now=2000.0)
    assert "run 42" in out and "Invoice chased" in out
    assert out.endswith("VERIFIED"), "the announcer keeps the TAIL — tag must be last"


def test_completed_without_report_is_unverified():
    out = osrun_outcome(SIDECAR, [_run("completed", report="  ")], now=2000.0)
    assert out.endswith("UNVERIFIED")


def test_failed_run_speaks_the_error_and_tags_failed():
    out = osrun_outcome(SIDECAR, [_run("failed", error="budget exceeded")], now=2000.0)
    assert "budget exceeded" in out
    assert out.endswith("FAILED")


def test_missing_run_waits_out_the_grace_window_then_gives_up_honestly():
    """A fresh delegation may not be visible yet; a stale one has fallen off the
    30-row /status window and must resolve UNVERIFIED, never hang silently."""
    fresh = SIDECAR["started"] + 30.0
    assert osrun_outcome(SIDECAR, [], now=fresh) is None
    stale = SIDECAR["started"] + OSRUN_WINDOW_GRACE_S + 1
    out = osrun_outcome(SIDECAR, [], now=stale)
    assert out is not None and out.endswith("UNVERIFIED")


def test_other_runs_do_not_match():
    other = dict(_run("completed", report="done"), id=99)
    assert osrun_outcome(SIDECAR, [other], now=2000.0) is None


def test_os_delegate_writes_the_sidecar(monkeypatch, tmp_path):
    """The tool must keep the runId: sidecar + prompt claim a tid in TASKS_DIR."""
    import config
    import tools
    monkeypatch.setattr(config, "TASKS_DIR", str(tmp_path))
    monkeypatch.setattr(config, "ensure_dirs", lambda: None)
    monkeypatch.setattr(kernel_tools, "_kernel_call",
                        lambda *a, **k: {"runId": 7, "accepted": True})
    out = kernel_tools.os_delegate({"instruction": "update the CRM"})
    assert "run 7" in out
    sidecars = list(tmp_path.glob("*.osrun"))
    assert len(sidecars) == 1
    data = json.loads(sidecars[0].read_text())
    assert data["runId"] == 7 and data["instruction"] == "update the CRM"
    assert abs(data["started"] - time.time()) < 5
    tid = sidecars[0].name[:-6]
    assert (tmp_path / f"{tid}.prompt").exists(), "prompt file claims the tid for de-collision"


def test_status_helper_does_not_shadow_the_spoken_tool():
    """kernel_status is a voice TOOL returning a sentence; the raw /status payload
    lives in kernel_status_raw. A later def silently shadowing the tool is exactly
    the bug this pins (it happened during the 2026-08-08 build)."""
    assert kernel_tools.kernel_status.__annotations__.get("return") is str
    assert kernel_tools.kernel_status_raw.__annotations__.get("return") is dict


def test_os_delegate_without_runid_falls_back_to_one_way(monkeypatch, tmp_path):
    import config
    monkeypatch.setattr(config, "TASKS_DIR", str(tmp_path))
    monkeypatch.setattr(kernel_tools, "_kernel_call", lambda *a, **k: {})
    out = kernel_tools.os_delegate({"instruction": "x"})
    assert "Telegram" in out
    assert not list(tmp_path.glob("*.osrun"))
