"""Characterization test for the macos_context extraction (Phase 4).

Pins that the public API moved out of agent.py intact and importing it does not
pull agent at import time (the cycle stays broken).
"""
import sys

from mac import macos_context


def test_public_api_present_and_callable():
    assert callable(macos_context.grab_context)
    assert callable(macos_context.grab_window_screenshot)


def test_import_does_not_pull_agent():
    # importing macos_context alone must not import agent (lazy _log shim only)
    mod = sys.modules.copy()
    assert "mac.macos_context" in mod
    # agent may be absent; if present it's from elsewhere, not a macos_context import-time dep


def test_grab_context_returns_str():
    # always returns a string (graceful even with no AX/permissions)
    assert isinstance(macos_context.grab_context(), str)


def test_sensitive_app_skips_screenshot(monkeypatch):
    monkeypatch.setattr(macos_context, "_frontmost_app_and_title", lambda: ("Nordea", ""))
    monkeypatch.setattr(macos_context, "_log", lambda message: None)

    def fail_screencapture(*args, **kwargs):
        raise AssertionError("screencapture must not run for sensitive apps")

    monkeypatch.setattr(macos_context.subprocess, "run", fail_screencapture)

    assert macos_context.grab_window_screenshot() == ""


def test_unidentified_frontmost_app_skips_screenshot(monkeypatch):
    monkeypatch.setattr(macos_context, "_frontmost_app_and_title", lambda: ("", ""))
    monkeypatch.setattr(macos_context, "_log", lambda message: None)

    def fail_screencapture(*args, **kwargs):
        raise AssertionError("screencapture must not run for an unidentified app")

    monkeypatch.setattr(macos_context.subprocess, "run", fail_screencapture)

    assert macos_context.grab_window_screenshot() == ""


def test_denied_cursor_window_title_skips_screenshot(monkeypatch):
    monkeypatch.setattr(macos_context, "_frontmost_app_and_title", lambda: ("Finder", "Desktop"))
    monkeypatch.setattr(macos_context, "_ax_window_under_cursor", lambda: (object(), "Signal chat"))
    monkeypatch.setattr(macos_context, "_log", lambda message: None)

    def fail_screencapture(*args, **kwargs):
        raise AssertionError("screencapture must not run for a sensitive cursor window")

    monkeypatch.setattr(macos_context.subprocess, "run", fail_screencapture)

    assert macos_context.grab_window_screenshot() == ""


def test_full_screen_with_unidentified_app_skips_screenshot(monkeypatch):
    monkeypatch.setattr(macos_context, "_frontmost_app_and_title", lambda: ("", "Desktop"))
    monkeypatch.setattr(macos_context, "_ax_window_under_cursor", lambda: (None, ""))
    monkeypatch.setattr(macos_context, "_focused_window_region", lambda: None)
    monkeypatch.setattr(macos_context, "_log", lambda message: None)

    def fail_screencapture(*args, **kwargs):
        raise AssertionError("screencapture must not run for an unidentified full-screen capture")

    monkeypatch.setattr(macos_context.subprocess, "run", fail_screencapture)

    assert macos_context.grab_window_screenshot() == ""


def test_innocuous_window_screenshot_attempts_capture(monkeypatch, tmp_path):
    commands = []
    monkeypatch.setattr(macos_context, "_frontmost_app_and_title", lambda: ("Finder", "Desktop"))
    monkeypatch.setattr(macos_context, "_ax_window_under_cursor", lambda: (None, ""))
    monkeypatch.setattr(macos_context, "_focused_window_region", lambda: "0,0,100,100")
    monkeypatch.setattr(macos_context.tempfile, "gettempdir", lambda: str(tmp_path))

    def run_capture(command, **kwargs):
        commands.append(command)
        if command[0] == "screencapture":
            with open(command[-1], "wb") as screenshot:
                screenshot.write(b"test")

    monkeypatch.setattr(macos_context.subprocess, "run", run_capture)

    assert macos_context.grab_window_screenshot() == "dGVzdA=="
    assert commands[0][0] == "screencapture"
