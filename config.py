#!/usr/bin/env python3
"""Per-user configuration + secret storage — the foundation for shipping this as a
product instead of a personal tool.

- Non-secret settings live in ~/Library/Application Support/ThrivbeVoice/config.json
- API keys live in the macOS **Keychain** (service "ThrivbeVoice"), never on disk.
- Memory + logs move out of /tmp and the Thrivbe workspace into the app's own dirs.

Dev continuity: `secret(name)` falls back to environment variables (which agent.py
still loads from ~/Thrivbe-AI/.env in dev), so the existing setup keeps working with
no keys re-entered. New users enter keys in onboarding → Keychain.

No third-party deps: Keychain access is via the `security` CLI.

Self-check:  python3 config.py --selftest
"""
import json
import os
import subprocess

APP_NAME = "ThrivbeVoice"
KEYCHAIN_SERVICE = APP_NAME

SUPPORT_DIR = os.path.expanduser(f"~/Library/Application Support/{APP_NAME}")
LOG_DIR = os.path.expanduser(f"~/Library/Logs/{APP_NAME}")
CONFIG_PATH = os.path.join(SUPPORT_DIR, "config.json")
MEMORY_PATH = os.path.join(SUPPORT_DIR, "memory.log")
LOG_PATH = os.path.join(LOG_DIR, "agent.log")

# Secret name -> env var(s) to fall back on when the Keychain has no entry (dev mode).
_ENV_FALLBACK = {
    "openai": ["OPENAI_API_KEY"],
    "openrouter": ["OPENROUTER_API", "OPENROUTER_API_KEY"],
    "elevenlabs": ["ELEVENLABS_API_KEY"],
}

DEFAULTS = {
    "onboarding_complete": False,
    "live": {
        "voice": "alloy",
        "agentic_shell": True,
        "shell_timeout": 20,
        "delegate": "pi",                 # "pi" | "claude" | "off"
        "pi_model": "deepseek-v4-flash",
        "workspace": None,                # optional context/home folder; None = $HOME
    },
    "ptt": {
        "voice_engine": "elevenlabs",     # "elevenlabs" | "say"
        "eleven_voice": "21m00Tcm4TlvDq8ikWAM",
        "brain_model": "openai/gpt-4o-mini",
    },
    "audio": {"input_device": None, "output_device": None},   # null = system default / smart pick
    "hotkeys": {"live": "double_ctrl", "ptt": "alt_r"},
    "privacy": {"read_cursor_context": True},
}


def ensure_dirs() -> None:
    for d in (SUPPORT_DIR, LOG_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass


def _deep_merge(base: dict, over: dict) -> dict:
    """Return base with over layered on top (recursively) — so new DEFAULTS keys
    appear automatically for users with an older config.json."""
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> dict:
    ensure_dirs()
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            user = json.load(f)
    except FileNotFoundError:
        user = {}
    except Exception:
        user = {}
    return _deep_merge(DEFAULTS, user)


def save(cfg: dict) -> None:
    ensure_dirs()
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)   # atomic


def get(path: str, default=None):
    """Dotted lookup, e.g. get('live.voice')."""
    node = load()
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def set_(path: str, value) -> dict:
    """Dotted set + persist; returns the new config."""
    cfg = load()
    node = cfg
    parts = path.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value
    save(cfg)
    return cfg


# ----- secrets (macOS Keychain via the `security` CLI) ----------------------
def secret(name: str) -> str | None:
    """Keychain first, then env fallback (dev). Returns None if nowhere."""
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", name, "-w"],
            capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    for env in _ENV_FALLBACK.get(name, []):
        v = os.environ.get(env)
        if v:
            return v
    return None


def set_secret(name: str, value: str) -> bool:
    """Store/overwrite a secret in the Keychain (-U updates if present)."""
    try:
        r = subprocess.run(
            ["security", "add-generic-password", "-U",
             "-s", KEYCHAIN_SERVICE, "-a", name, "-w", value],
            capture_output=True, text=True)
        return r.returncode == 0
    except Exception:
        return False


def delete_secret(name: str) -> None:
    try:
        subprocess.run(
            ["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", name],
            capture_output=True, text=True)
    except Exception:
        pass


def has_required_keys() -> bool:
    """OpenAI is the one mandatory key (live voice + transcription)."""
    return bool(secret("openai"))


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        # deep-merge surfaces new keys without clobbering user values
        merged = _deep_merge(DEFAULTS, {"live": {"voice": "echo"}})
        assert merged["live"]["voice"] == "echo"
        assert merged["live"]["agentic_shell"] is True
        assert merged["ptt"]["voice_engine"] == "elevenlabs"
        # secret round-trip (uses a throwaway account so it can't touch real keys)
        tname = "selftest-tmp"
        assert set_secret(tname, "hunter2"), "Keychain write failed"
        assert secret(tname) == "hunter2", "Keychain read mismatch"
        delete_secret(tname)
        assert secret(tname) is None, "Keychain delete failed"
        print("CONFIG OK — dir:", SUPPORT_DIR)
    else:
        print("config for", APP_NAME)
        print("  support dir:", SUPPORT_DIR)
        print("  openai key present:", has_required_keys())
