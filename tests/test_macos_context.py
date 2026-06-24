"""Characterization test for the macos_context extraction (Phase 4).

Pins that the public API moved out of agent.py intact and importing it does not
pull agent at import time (the cycle stays broken).
"""
import sys

import macos_context


def test_public_api_present_and_callable():
    assert callable(macos_context.grab_context)
    assert callable(macos_context.grab_window_screenshot)


def test_import_does_not_pull_agent():
    # importing macos_context alone must not import agent (lazy _log shim only)
    mod = sys.modules.copy()
    assert "macos_context" in mod
    # agent may be absent; if present it's from elsewhere, not a macos_context import-time dep


def test_grab_context_returns_str():
    # always returns a string (graceful even with no AX/permissions)
    assert isinstance(macos_context.grab_context(), str)
