"""Realtime tool schema + the helpers behind the tools.

Extracted from realtime.py: the flat Realtime `TOOLS` schema and the standalone
helpers for delegation (background agent), clipboard/paste, and JSON extraction.
LiveSession._do_tool dispatches to these.
"""
import json
import os
import shlex
import subprocess
import time

import config


def _log(msg: str) -> None:
    try:
        from agent import LOG
        LOG(f"tools: {msg}")
    except Exception:
        pass


# Realtime tool schema is flat (name/parameters at top level), unlike the
# chat-completions nested {"function": {...}} shape in agent.py.
TOOLS = [
    {
        "type": "function",
        "name": "run_shell",
        "description": "Run a command in the persistent shell (starts in Robin's configured base folder; cd/env persist; can act anywhere on the Mac). Use it to read, search, and act. To hand off a slow coding/research task, ALWAYS use the `delegate` tool (it runs `pi` in the background and auto-wakes with the result) — do NOT shell out to `claude` or `pi` yourself.",
        "parameters": {"type": "object",
                       "properties": {"command": {"type": "string"}},
                       "required": ["command"]},
    },
    {
        "type": "function",
        "name": "remember",
        "description": "Explicitly save one durable fact or preference about the user into your long-term memory (you already learn most things automatically — use this only when the user clearly wants something kept). Don't use it to re-store something you just recalled.",
        "parameters": {"type": "object",
                       "properties": {"note": {"type": "string"}},
                       "required": ["note"]},
    },
    {
        "type": "function",
        "name": "recall",
        "description": "Search your memory — past conversations and the user's wider knowledge — for relevant context. Use it when the user refers to something from before, or when you need background you don't already have in this conversation.",
        "parameters": {"type": "object",
                       "properties": {"query": {"type": "string"}},
                       "required": ["query"]},
    },
    {
        "type": "function",
        "name": "put_text",
        "description": "Put text into the app the user is in: copies it to the clipboard and (by default) pastes it into the frontmost window with Cmd-V. Use for writing/inserting/pasting into emails, docs, fields.",
        "parameters": {"type": "object",
                       "properties": {"text": {"type": "string"},
                                      "paste": {"type": "boolean",
                                                "description": "true (default) = paste into the front window; false = only copy to clipboard"}},
                       "required": ["text"]},
    },
    {
        "type": "function",
        "name": "delegate",
        "description": "Hand a slow coding/research task to a background AI agent. Returns immediately and survives the conversation; when it finishes the voice agent automatically comes back and speaks the result. Don't wait or poll.",
        "parameters": {"type": "object",
                       "properties": {"instruction": {"type": "string"}},
                       "required": ["instruction"]},
    },
    {
        "type": "function",
        "name": "set_prompt",
        "description": "Save (overwrite) your persistent custom instructions — your persona and standing context about the user and their workspace. Reloaded at the start of every future conversation. Use when the user asks you to remember how to behave, or after researching their setup to write your own briefing.",
        "parameters": {"type": "object",
                       "properties": {"text": {"type": "string"}},
                       "required": ["text"]},
    },
]


def _build_delegate_cmd(instruction: str, cfg: dict):
    """Write the instruction to a prompt file and return (shell command, out_path) that
    runs the background agent DETACHED, capturing output to TASKS_DIR/<id>.out and
    dropping a .done sentinel on completion (the menubar watcher polls for it to
    auto-wake and speak the result). Returns None if delegation is off or empty."""
    instruction = (instruction or "").strip()
    live = cfg.get("live") or {}
    mode = live.get("delegate", "pi")
    if not instruction or mode == "off":
        return None
    if mode == "claude":
        agent_cmd = "claude -p"
    else:
        agent_cmd = f"pi -p --model {live.get('pi_model', 'deepseek-v4-flash')}"
    try:
        config.ensure_dirs()
        tid = time.strftime("%H%M%S")
        pf = os.path.join(config.TASKS_DIR, f"{tid}.prompt")
        out = os.path.join(config.TASKS_DIR, f"{tid}.out")
        done = os.path.join(config.TASKS_DIR, f"{tid}.done")
        with open(pf, "w", encoding="utf-8") as f:
            f.write(instruction)
        # $(cat prompt) avoids any shell-injection from the instruction text itself.
        # </dev/null is essential: detached under the live shell, the agent would
        # otherwise inherit an open stdin that never EOFs and block forever (0% CPU,
        # no output, no .done — so the auto-wake never fires).
        runner = (f'{agent_cmd} "$(cat {shlex.quote(pf)})" </dev/null > {shlex.quote(out)} 2>&1; '
                  f'touch {shlex.quote(done)}')
        if live.get("show_task_terminals"):
            # Open the agent INTERACTIVELY in its own Terminal (real TTY → the full live
            # agent UI: tool calls, streaming, progress). The instruction is passed as the
            # initial message, exactly as if the user typed it. NO `-p` and NO pipe — a pipe
            # strips the TTY and pi falls back to writing only its final answer (which is why
            # tee/tail looked frozen). Trade-off: pi stays open for you to watch, so there is
            # no auto-captured result for the menubar watcher to speak — watching IS the UX.
            interactive_cmd = agent_cmd.replace("pi -p", "pi").replace("claude -p", "claude")
            term = f'{interactive_cmd} "$(cat {shlex.quote(pf)})"'
            osa = f'tell application "Terminal" to do script {json.dumps(term)}'
            cmd = f"osascript -e {shlex.quote(osa)} >/dev/null 2>&1"
        else:
            # Headless: detached, no window, auto-wakes + speaks the result. </dev/null so
            # it doesn't block on stdin.
            cmd = f"nohup sh -c {shlex.quote(runner)} >/dev/null 2>&1 & disown"
        # Return the out-path too so the caller can open a live log window on it.
        return (cmd, out)
    except Exception as e:
        _log(f"delegate build failed: {e!r}")
        return None


def _extract_json(text: str):
    """Best-effort: pull the first JSON object out of a model's output (it may wrap it
    in prose or ```json fences). Returns the parsed dict, or None if there's no valid
    object — callers degrade to treating the raw text as a plain summary."""
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except Exception:
        return None


def _put_text(text: str, paste: bool = True) -> str:
    """Copy text to the clipboard and (by default) paste it into the frontmost window.
    Copy always works; auto-paste needs Accessibility — if it can't, the text is still
    on the clipboard and we tell the user to press Cmd-V."""
    text = text or ""
    if not text.strip():
        return "nothing to put"
    try:
        subprocess.run(["pbcopy"], input=text, text=True, check=True)
    except Exception as e:
        return f"couldn't reach the clipboard: {e}"
    if not paste:
        return "copied to the clipboard"
    r = subprocess.run(
        ["osascript", "-e",
         'tell application "System Events" to keystroke "v" using command down'],
        capture_output=True, text=True)
    if r.returncode != 0:
        return "copied to the clipboard — press Cmd-V to paste it in (auto-paste needs Accessibility permission)"
    return "pasted it into the front window"
