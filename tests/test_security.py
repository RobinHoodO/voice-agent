"""Phase 1 security regression tests — each pins a fixed vulnerability.

If any of these fail, a P0 hole has reopened.
"""
import os
import stat

import config
import memory
import settings


# --- 1.1 openPane shell-arg whitelist ---------------------------------------
def test_openpane_allows_known_anchor(monkeypatch):
    calls = []
    monkeypatch.setattr(settings.subprocess, "run", lambda *a, **k: calls.append(a))
    settings._handle("openPane", "Privacy_Microphone")
    assert calls, "legit anchor should open the pane"
    assert "Privacy_Microphone" in calls[0][0][1]


def test_openpane_rejects_injected_arg(monkeypatch):
    calls = []
    monkeypatch.setattr(settings.subprocess, "run", lambda *a, **k: calls.append(a))
    # an attacker-controlled bridge message trying to smuggle a second URL/command
    settings._handle("openPane", "Privacy_Microphone&x-evil://pwn")
    assert not calls, "non-whitelisted arg must not reach `open`"


# --- 1.2 secrets not inherited by the agentic shell -------------------------
def test_subprocess_env_strips_secrets(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("SOME_TOKEN", "t0ken")
    monkeypatch.setenv("DB_PASSWORD", "hunter2")
    monkeypatch.setenv("DEPLOY_PRIVATE_KEY", "-----BEGIN")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = config.subprocess_env()
    for leaked in ("OPENAI_API_KEY", "SOME_TOKEN", "DB_PASSWORD", "DEPLOY_PRIVATE_KEY"):
        assert leaked not in env, f"{leaked} leaked into the agentic shell"
    assert env.get("PATH") == "/usr/bin"   # non-secret env preserved


# --- 1.3 secret files are not world-readable --------------------------------
def test_config_and_db_are_owner_only(tmp_app):
    config.set_("live.voice", "alloy")
    cfg_mode = stat.S_IMODE(os.stat(config.CONFIG_PATH).st_mode)
    assert cfg_mode == 0o600, f"config.json is {oct(cfg_mode)}, expected 0o600"
    dir_mode = stat.S_IMODE(os.stat(config.SUPPORT_DIR).st_mode)
    assert dir_mode == 0o700, f"support dir is {oct(dir_mode)}, expected 0o700"

    memory.record("transcript", summary="s")
    db_mode = stat.S_IMODE(os.stat(memory.DB_PATH).st_mode)
    assert db_mode == 0o600, f"conversations.db is {oct(db_mode)}, expected 0o600"


# --- 1.4 recall command injection -------------------------------------------
def test_recall_command_no_injection(tmp_app):
    sentinel = tmp_app / "pwned"
    config.set_("live.memory.command", "echo {query}")
    malicious = f"; touch {sentinel}"   # no quotes in the template → bare injection
    memory._recall_command(malicious, 5)
    assert not sentinel.exists(), "query must not break out of the KB command"
