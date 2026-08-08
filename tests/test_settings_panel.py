"""Settings panel: quiet-hours time validation + the Tools inventory it renders."""
import sys
import types

import pytest

# settings.py imports Cocoa/WebKit at module scope; stub them so the test runs headless.
for name in ("Cocoa", "WebKit"):
    sys.modules.setdefault(name, types.SimpleNamespace(
        **{k: object for k in ("NSObject", "NSWindow", "NSBackingStoreBuffered",
                               "NSMakeRect", "NSApp", "NSWindowStyleMaskTitled",
                               "NSWindowStyleMaskClosable", "NSWindowStyleMaskResizable",
                               "NSWindowStyleMaskMiniaturizable", "WKWebView",
                               "WKWebViewConfiguration")}))

import settings  # noqa: E402


@pytest.mark.parametrize("raw,want", [
    ("09:00", "09:00"), ("9:5", "09:05"), ("00:00", "00:00"), ("23:59", "23:59"),
    ("", None), ("24:00", None), ("07:60", None), ("nope", None), (None, None),
])
def test_hhmm_only_accepts_real_clock_times(raw, want):
    assert settings._hhmm(raw) == want


def test_tools_lists_every_tool_and_gates_shell(monkeypatch):
    import tools as tools_mod

    monkeypatch.setattr(settings.config, "get",
                        lambda path, default=None: False if path == "live.agentic_shell" else default)
    listed = settings._tools()
    assert len(listed) == len(tools_mod.TOOLS)
    assert [t["name"] for t in listed] == sorted(t["name"] for t in listed)
    by_name = {t["name"]: t for t in listed}
    assert by_name["run_shell"]["off"] and by_name["delegate"]["off"]
    assert not by_name["recall"]["off"]
    assert by_name["gmail_send"]["confirm"]

    monkeypatch.setattr(settings.config, "get",
                        lambda path, default=None: True if path == "live.agentic_shell" else default)
    assert not {t["name"]: t for t in settings._tools()}["run_shell"]["off"]
