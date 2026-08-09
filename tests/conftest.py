"""Shared test fixtures.

The app stores config + the conversation DB under ~/Library/Application Support.
Tests must never touch the real ones, so `tmp_app` redirects every config path
(and memory's module-global DB_PATH) at a throwaway tmp dir per test.
"""
import importlib

import pytest


@pytest.fixture
def tmp_app(monkeypatch, tmp_path):
    """Point config + memory storage at an isolated tmp dir. Yields the dir."""
    from core import config

    support = tmp_path / "support"
    logs = tmp_path / "logs"
    monkeypatch.setattr(config, "SUPPORT_DIR", str(support))
    monkeypatch.setattr(config, "LOG_DIR", str(logs))
    monkeypatch.setattr(config, "CONFIG_PATH", str(support / "config.json"))
    monkeypatch.setattr(config, "MEMORY_PATH", str(support / "memory.log"))
    monkeypatch.setattr(config, "LOG_PATH", str(logs / "agent.log"))
    monkeypatch.setattr(config, "ACTIVITY_PATH", str(logs / "activity.log"))
    monkeypatch.setattr(config, "TASKS_DIR", str(support / "tasks"))
    monkeypatch.setattr(config, "_cache", None)   # don't leak a cached config between tests

    # memory.DB_PATH is captured at import from config.SUPPORT_DIR — re-point it.
    memory = importlib.import_module("core.memory")
    monkeypatch.setattr(memory, "DB_PATH", str(support / "conversations.db"))

    return support
