#!/usr/bin/env python3
"""Per-user configuration + secret storage — the foundation for shipping this as a
product instead of a personal tool.

- Non-secret settings live in ~/Library/Application Support/ThrivbeVoice/config.json
- API keys live in the macOS **Keychain** (service "ThrivbeVoice"), never on disk.
- Memory + logs move out of /tmp and the Thrivbe workspace into the app's own dirs.

Dev continuity: `secret(name)` falls back to environment variables (which agent.py
still loads from ~/Thrivbe-AI/.env in dev), so the existing setup keeps working with
no keys re-entered. New users enter keys in onboarding → Keychain.

No third-party deps: the Keychain store lives in `mac.keychain` and registers itself
into `core.secrets`; this module only knows "ask the store, then the env fallback".

Self-check:  python3 -m core.config --selftest   (from the repo root)
"""
import json
import os
import threading
import time

from core import secrets

APP_NAME = "ThrivbeVoice"
KEYCHAIN_SERVICE = APP_NAME

SUPPORT_DIR = os.path.expanduser(f"~/Library/Application Support/{APP_NAME}")
LOG_DIR = os.path.expanduser(f"~/Library/Logs/{APP_NAME}")
CONFIG_PATH = os.path.join(SUPPORT_DIR, "config.json")
MEMORY_PATH = os.path.join(SUPPORT_DIR, "memory.log")
LOG_PATH = os.path.join(LOG_DIR, "agent.log")
ACTIVITY_PATH = os.path.join(LOG_DIR, "activity.log")   # curated, human-readable feed
TASKS_DIR = os.path.join(SUPPORT_DIR, "tasks")          # detached job output + .done sentinels


def activity(msg: str) -> None:
    """Append one human-readable line to the Activity feed shown in the pop-up window.
    The raw, noisy log (LOG_PATH) stays separate for diagnostics."""
    try:
        ensure_dirs()
        with open(ACTIVITY_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')}  {msg}\n")
    except Exception:
        pass


def reset_activity() -> None:
    """Clear the feed at the start of a conversation so the window shows only this one."""
    try:
        ensure_dirs()
        open(ACTIVITY_PATH, "w").close()
    except Exception:
        pass

# Secret name -> env var(s) to fall back on when the Keychain has no entry (dev mode).
# Live mode only needs OpenAI (Realtime + transcription).
_ENV_FALLBACK = {
    "openai": ["OPENAI_API_KEY"],
    "VOICE_API_TOKEN": ["VOICE_API_TOKEN"],
    "notion": ["NOTION_KEY"],          # direct Notion REST (services.py)
    # The reverse channel's shared token (mac/reverse_channel.py). Listed here for two
    # reasons: the env fallback keeps dev working without a Keychain write, and being
    # in this table is what puts it in `_SECRET_ENV_VARS` — so a command the reverse
    # channel itself runs cannot read the token that authorised it.
    "reverse_channel_token": ["VOICE_AGENT_REVERSE_TOKEN"],
    "front": ["FRONT_API_TOKEN"],      # Front scripts read it themselves; listed here so
                                       # it's scrubbed from the agentic shell's env too
}

# Env var names that hold secrets — never expose these to spawned shells/subprocesses.
_SECRET_ENV_VARS = {e for envs in _ENV_FALLBACK.values() for e in envs}


def subprocess_env() -> dict:
    """os.environ minus secrets — for the agentic shell, which runs model-authored
    commands and must not be able to read API keys (e.g. `echo $OPENAI_API_KEY`)."""
    env = dict(os.environ)
    for k in list(env):
        if k in _SECRET_ENV_VARS or k.endswith(
                ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_PRIVATE_KEY", "_CREDENTIAL")):
            env.pop(k, None)
    return env

DEFAULTS = {
    "onboarding_complete": False,
    "live": {
        "voice": "alloy",
        "backend": "openai",             # "openai" | "gemini"
        # Speech-recognition language hints. Without these Gemini auto-detects per
        # utterance and short ones land as Spanish/other; first entry is primary.
        "languages": ["en-US", "nb-NO"],
        "agentic_shell": False,           # off by default — running shell is opt-in (menu toggle)
        "shell_timeout": 20,
        "delegate": "pi",                 # "pi" | "claude" | "off"
        "pi_model": "deepseek-v4-flash",
        "show_task_terminals": False,     # run delegated tasks as visible herdr lanes (False = headless)
        "workspace": None,                # optional context/home folder; None = $HOME
        "custom_prompt": "",              # user/agent-authored persona+context, reloaded every session
        "focus": None,                    # {subject, name, dir} — current client/project (focus.py)
        "focus_budget": 150000,           # chars of folder text loaded per focus (~40k tokens)
        "vad": {                          # local voice-detection (Gemini backend), per-mic
            "threshold": 0.10,                # level that starts a turn when she's silent
            "threshold_while_speaking": 0.28, # higher bar to INTERRUPT her — rejects headset echo
            "barge_in_hold_sec": 0.25,        # loud input must persist this long to interrupt
            "silence_sec": 1.5,               # gap that ends a turn — short values answer mid-sentence
        },
        # Stall escalation — silence is ambiguous (thinking / looping / dead all sound the
        # same). Seconds of NO audio on a turn before each cue; 0 disables that step.
        "stall_tone_s": 6,                # soft "still working" blip
        "stall_nudge_s": 15,              # ask her to say one sentence out loud
        "stall_reconnect_s": 35,          # presume the socket is dead; drop and reconnect
        "memory": {                       # conversation recall + learning — see memory.py
            "provider": "none",           # "none" | "claude-mem" | "command"
            "recall_count": 5,
            "claude_mem_db": "~/.claude-mem/claude-mem.db",
            "command": None,              # custom KB query, e.g. "my-kb search {query}"
            "learn": True,                # extract durable learnings on conversation close
            "mirror_claude_mem": False,   # also write learnings into claude-mem (off by default)
        },
        "quiet_hours": {                  # suppress auto-wake-and-speak on finished bg tasks
            "enabled": True,
            "start": "00:00",             # HH:MM, local time
            "end": "07:00",
            "weekdays_only": True,        # Mon-Fri only; False = every day
        },
    },
    "audio": {"input_device": None, "output_device": None},   # device-name substring; null = smart default
    "hotkeys": {"live": "double_ctrl"},
    "privacy": {
        "read_cursor_context": True,       # text under the mouse cursor + marked selection
        "read_window_context": False,      # full focused-window text (larger context, via AX)
        "read_window_screenshot": False,   # send a screenshot of the focused window (vision)
    },
    "ui": {"show_terminal": False},        # open a Terminal tailing the log during live mode
    "system": {"open_at_login": False},
    # The phone surface (server/ + mac/phone_surface.py): Robin's iPhone as a remote
    # microphone and speaker for THIS process. Off until he turns it on from the menu —
    # it is a listener, and a listener nobody asked for is not a feature.
    "phone_surface": {
        "enabled": False,
        # The HTTP listener. Loopback only by default; `tailscale serve` reaches it from
        # there. `mac.tailnet.resolve_bind_host` refuses anything that is not loopback or
        # a tailnet address this Mac holds — 0.0.0.0 is not a value this accepts.
        "bind": "127.0.0.1",
        "port": 8767,
        # Where TLS is terminated on the tailnet. NOT 443: this Mac already serves 443
        # to trustmux on 127.0.0.1:7432, and serving there would replace it.
        "tls_port": 8443,
        # Concurrent phone tabs. The floor (core/floor.py) means only one CONVERSATION
        # runs at a time anyway; this is the cap on sockets, so a phone that reconnects
        # in a tunnel cannot pile up.
        "max_sessions": 2,
    },
    # The reverse channel: phone-Pam acting ON this Mac (mac/reverse_channel.py).
    # OFF, and it stays off until Robin turns it on — this is the widest attack
    # surface in the build, and a default-on remote executor on a laptop is not a
    # feature, it is a finding. Nothing listens while `enabled` is false.
    "reverse_channel": {
        "enabled": False,
        "port": 8791,
        # Which tailnet address to bind. None = whichever one Tailscale reports.
        # Pinning may only NARROW the choice: a non-tailnet value is refused, it is
        # never honoured (mac/reverse_channel.resolve_bind_host).
        "bind": None,
        # Where run_shell and open_file are allowed to act. None = ~/Thrivbe-AI.
        # A path outside this root is refused before the gate is even consulted, and
        # run_shell is additionally jailed to it by a seatbelt profile
        # (mac/shell_sandbox.py) — the text scan cannot see through `$HOME` or a
        # symlink, and the kernel can.
        "workspace": None,
        "shell_timeout": 20,
        # May the jailed shell reach the network? OFF: a sandbox that still allows
        # POST is a jail with a mail slot. Turn it on only if `git pull` from the
        # phone is worth it — the workspace is readable to the caller either way,
        # so this only widens where its contents can be sent.
        "sandbox_network": False,
        # Which apps the reverse channel may screenshot, by exact frontmost-app name.
        # None = the built-in work-surface list in mac/reverse_channel.py. This is an
        # ALLOW-list on purpose: macos_context's denylist cannot fail closed for an app
        # nobody has met yet, and down a wire it has to.
        "screenshot_apps": None,
    },
}

# The default root for the reverse channel when `reverse_channel.workspace` is unset.
# Not `live.workspace`: that one is a CONTEXT hint the agent starts its shell in and
# is routinely None (meaning $HOME). A remote executor whose scope silently means
# "the whole home folder" is not scoped at all.
REVERSE_CHANNEL_DEFAULT_WORKSPACE = "~/Thrivbe-AI"


def ensure_dirs() -> None:
    for d in (SUPPORT_DIR, LOG_DIR, TASKS_DIR):
        try:
            os.makedirs(d, exist_ok=True)
            os.chmod(d, 0o700)   # owner-only: holds transcripts, logs, job output
        except Exception:
            pass


def _deep_merge(base: dict, over: dict) -> dict:
    """Return base with over layered on top (recursively) — so new DEFAULTS keys
    appear automatically for users with an older config.json.

    Every nested dict is COPIED, including the ones `over` says nothing about. A plain
    `dict(base)` aliases them, and since `load()` merges onto DEFAULTS that made the
    live config share objects with the shipped defaults: `set_("reverse_channel.enabled",
    True)` reached through the alias and rewrote `DEFAULTS` itself, so the one thing that
    is supposed to be constant — what this app ships as OFF — changed at runtime and
    nothing could be asked about it afterwards.
    """
    out = {k: (_deep_merge(v, {}) if isinstance(v, dict) else v)
           for k, v in (base or {}).items()}
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


_cache = None                     # merged config, loaded once and reused
_cache_lock = threading.Lock()    # serialize set_ so concurrent writers don't clobber


def load() -> dict:
    """Merged (DEFAULTS + user) config. Cached after the first read so config.json is
    parsed from disk once, not on every get(). save()/set_ keep the cache consistent."""
    global _cache
    if _cache is None:
        ensure_dirs()
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                user = json.load(f)
        except FileNotFoundError:
            user = {}
        except Exception:
            user = {}
        _cache = _deep_merge(DEFAULTS, user)
    return _cache


def save(cfg: dict) -> None:
    global _cache
    ensure_dirs()
    tmp = CONFIG_PATH + ".tmp"
    # Create the temp file owner-only from the start (no 0644 window before replace);
    # config may hold workspace paths / the custom prompt.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)   # atomic; 0600 carried over from the fd
    _cache = cfg                   # cache reflects what we just wrote


def get(path: str, default=None):
    """Dotted lookup, e.g. get('live.voice')."""
    node = load()
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def set_(path: str, value) -> dict:
    """Dotted set + persist; returns the new config. Locked so the settings webview
    thread and the set_prompt tool can't clobber each other (last-writer-wins)."""
    with _cache_lock:
        cfg = load()
        node = cfg
        parts = path.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
        save(cfg)
        return cfg


# ----- secrets --------------------------------------------------------------
# The store is a capability: `mac.keychain` registers the macOS Keychain (the
# `security` CLI) at startup; a headless surface registers nothing and falls
# through to the env fallback below / systemd credentials. Order is unchanged.
def secret(name: str) -> str | None:
    """Store (Keychain on Mac) first, then env fallback (dev). None if nowhere."""
    return secrets.secret(name, _ENV_FALLBACK.get(name, []))


def set_secret(name: str, value: str) -> bool:
    """Store/overwrite a secret in the registered store. False if there isn't one."""
    return secrets.set_secret(name, value)


def delete_secret(name: str) -> None:
    secrets.delete_secret(name)


def has_required_keys() -> bool:
    """OpenAI is the one mandatory key (live voice + transcription)."""
    return bool(secret("openai"))


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        # deep-merge surfaces new keys without clobbering user values
        merged = _deep_merge(DEFAULTS, {"live": {"voice": "echo"}})
        assert merged["live"]["voice"] == "echo"                  # override applied
        assert merged["live"]["agentic_shell"] is False           # sibling default not clobbered
        assert merged["live"]["memory"]["recall_count"] == 5      # nested default preserved
        # secret round-trip (uses a throwaway account so it can't touch real keys).
        # The surface owns the store, so install the Mac one explicitly here.
        from mac.keychain import install as _install_keychain
        _install_keychain()
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
