#!/usr/bin/env python3
"""Settings state that has nothing to do with the window it is rendered in.

The macOS preferences window (`mac.settings`) is a WKWebView over settings.html; a
headless surface would render the same values as JSON. Everything both need — the
voice list, the clock-time validator that keeps quiet hours parseable, the memory
counters, and the Tools inventory with its live gating — lives here.

What stayed in `mac.settings`: the NSWindow/WKWebView, the JS bridge, the native
folder picker, the audio device test, and the login item.
"""
import subprocess
import time

from core import caps, config

VOICES = ["marin", "cedar", "alloy", "ash", "ballad", "coral",
          "echo", "sage", "shimmer", "verse"]
VERSION = "1.0"


def _log(msg: str) -> None:
    caps.log(f"settings: {msg}")


def _hhmm(value):
    """Normalize a bridge-supplied clock time to 'HH:MM', or None if it isn't one."""
    try:
        h, m = str(value).split(":")[:2]
        h, m = int(h), int(m)
    except Exception:
        return None
    return f"{h:02d}:{m:02d}" if 0 <= h <= 23 and 0 <= m <= 59 else None


def _greeting() -> str:
    h = int(time.strftime("%H"))
    part = "morning" if h < 12 else "afternoon" if h < 18 else "evening"
    try:
        # BSD `id -F` = full name. Absent elsewhere; the greeting just drops the name.
        name = subprocess.run(["id", "-F"], capture_output=True, text=True).stdout.strip()
        first = (name.split() or [""])[0]
    except Exception:
        first = ""
    return f"Good {part}" + (f", {first}" if first else "")


def _mem_stats() -> dict:
    """Live memory counts + recent learnings for the Settings panel (display-only).
    Isolated + non-fatal: a memory hiccup must never break the Settings window."""
    try:
        from core import memory
        s = memory.panel_stats(4)
        return {"mem_conversation_count": s["conversations"],
                "mem_learning_count": s["learnings"],
                "mem_recent_learnings": s["recent"]}
    except Exception as e:
        _log(f"mem stats failed: {e!r}")
        return {"mem_conversation_count": 0, "mem_learning_count": 0,
                "mem_recent_learnings": []}


def _tools() -> list:
    """Every tool the live agent can call, with the gating that applies right now, for
    the Tools panel. Mirrors live_session's own filter (agentic_shell gates run_shell +
    delegate) and its confirm gate (the kernel's declared high-stakes list, failing
    closed on every kernel tool when the manifest is unreadable — same as _configure).
    ponytail: 1s manifest timeout, not the session's 3s — this runs on the UI thread."""
    try:
        from core import kernel_tools
        from core import tools as tools_mod
        shell_on = bool(config.get("live.agentic_shell", False))
        declared = kernel_tools.kernel_high_stakes(timeout=1)
        high = set(kernel_tools.KERNEL_TOOL_NAMES if declared is None else declared)
        high |= set(tools_mod.LOCAL_HIGH_STAKES)
        out = []
        for t in tools_mod.TOOLS:
            name = t.get("name", "")
            props = (t.get("parameters") or {}).get("properties") or {}
            out.append({"name": name,
                        "description": t.get("description", ""),
                        "args": sorted(props),
                        "off": not shell_on and name in ("run_shell", "delegate"),
                        "confirm": name in high})
        return sorted(out, key=lambda t: t["name"])
    except Exception as e:
        _log(f"tool list failed: {e!r}")
        return []


def state(mics, spk, live_on: bool) -> dict:
    """Everything the settings UI renders. The surface supplies what only it can know:
    the device lists and whether a conversation is running right now."""
    return {
        "greeting": _greeting(),
        "version": VERSION,
        "mics": mics, "spk": spk, "voices": VOICES,
        "mic": config.get("audio.input_device"),
        "speaker": config.get("audio.output_device"),
        "backend": config.get("live.backend", "openai"),
        "voice": config.get("live.voice", "alloy"),
        "workspace": config.get("live.workspace", "") or "",
        "mem_provider": config.get("live.memory.provider", "none"),
        "mem_recall_count": config.get("live.memory.recall_count", 5),
        "mem_claude_db": config.get("live.memory.claude_mem_db", "") or "",
        "mem_command": config.get("live.memory.command", "") or "",
        "mem_learn": bool(config.get("live.memory.learn", True)),
        "mem_mirror": bool(config.get("live.memory.mirror_claude_mem", False)),
        **_mem_stats(),
        "custom_prompt": config.get("live.custom_prompt", "") or "",
        "deep_context": bool(config.get("privacy.read_cursor_context", True)),
        "window_context": bool(config.get("privacy.read_window_context", False)),
        "window_screenshot": bool(config.get("privacy.read_window_screenshot", False)),
        "agentic_shell": bool(config.get("live.agentic_shell", False)),
        "show_task_terminals": bool(config.get("live.show_task_terminals", False)),
        "delegate": config.get("live.delegate", "pi"),
        "proactive_wake": bool(config.get("live.proactive_wake", False)),
        "quiet_enabled": bool(config.get("live.quiet_hours.enabled", True)),
        "quiet_start": config.get("live.quiet_hours.start", "00:00"),
        "quiet_end": config.get("live.quiet_hours.end", "07:00"),
        "quiet_weekdays": bool(config.get("live.quiet_hours.weekdays_only", True)),
        "tools": _tools(),
        "activity_window": bool(config.get("ui.show_terminal", False)),
        "open_at_login": bool(config.get("system.open_at_login", False)),
        "live_on": bool(live_on),
        "key_present": bool(config.secret("openai")),
        "gemini_key_present": bool(config.secret("gemini")),
        "hotkey": "Double-tap Control",
    }
