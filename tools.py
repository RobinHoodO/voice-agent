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
    {
        "type": "function",
        "name": "kernel_status",
        "description": "Check what needs Robin's attention right now: pending approvals awaiting his decision, attention items surfaced by the OS, and recent agent runs. Use this for questions like what needs my attention, what's pending, or what has the OS been doing.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "kernel_decide",
        "description": "Approve or reject a pending kernel approval by id. Never call this based on inference. Only after you have read the approval's preview aloud and the user gave an explicit yes or no.",
        "parameters": {"type": "object",
                       "properties": {
                           "approvalId": {"type": ["integer", "string"],
                                          "description": "The approval id from a prior kernel_status call."},
                           "decision": {"type": "string", "enum": ["approve", "reject"],
                                        "description": "Robin's explicit yes/no decision."}},
                       "required": ["approvalId", "decision"]},
    },
    {
        "type": "function",
        "name": "kernel_memo",
        "description": "File a short note into Robin's unified inbox. Use for requests like note that down, remind me, or add this to my inbox.",
        "parameters": {"type": "object",
                       "properties": {"text": {"type": "string",
                                               "description": "The memo text, 1-4000 characters."}},
                       "required": ["text"]},
    },
    {
        "type": "function",
        "name": "bloom_create_task",
        "description": "Request creation of a new task in a Bloom project. This is gated: it files an approval for Robin and does not take effect until he approves it — tell him that plainly rather than saying the task is created.",
        "parameters": {"type": "object",
                       "properties": {
                           "projectId": {"type": ["integer", "string"],
                                         "description": "The Bloom project id to add the task to."},
                           "title": {"type": "string", "description": "The task title."},
                           "description": {"type": "string",
                                           "description": "Optional task description."}},
                       "required": ["projectId", "title"]},
    },
    {
        "type": "function",
        "name": "bloom_update_task",
        "description": "Request an update to an existing Bloom task, such as status or title. This is gated: it files an approval for Robin and does not take effect until he approves it — tell him that plainly rather than saying the task is updated.",
        "parameters": {"type": "object",
                       "properties": {
                           "id": {"type": ["integer", "string"], "description": "The Bloom task id."},
                           "status": {"type": "string", "enum": ["todo", "in_progress", "done"],
                                      "description": "Optional new task status."},
                           "title": {"type": "string", "description": "Optional new task title."}},
                       "required": ["id"]},
    },
    {
        "type": "function",
        "name": "bloom_comment_task",
        "description": "Request adding a comment to an existing Bloom task. This is gated: it files an approval for Robin and does not take effect until he approves it — tell him that plainly rather than saying the comment is posted.",
        "parameters": {"type": "object",
                       "properties": {
                           "id": {"type": ["integer", "string"], "description": "The Bloom task id."},
                           "body": {"type": "string", "description": "The comment text."}},
                       "required": ["id", "body"]},
    },
    {
        "type": "function",
        "name": "twenty_search_contacts",
        "description": "Search for people by first or last name in Twenty CRM.",
        "parameters": {"type": "object",
                       "properties": {"name_query": {"type": "string",
                                                     "description": "The name or partial name to search for."}},
                       "required": ["name_query"]},
    },
    {
        "type": "function",
        "name": "semsearch_query",
        "description": "Run semantic search against Robin's LinkedIn connections (people), Notion knowledge base (notion), or Obsidian wiki and agent skills (wiki_skills). Use wiki_skills for books, book summaries, abstracts, wiki pages, and skills.",
        "parameters": {"type": "object",
                       "properties": {
                           "query": {"type": "string", "description": "The natural language query."},
                           "corpus": {"type": "string",
                                      "enum": ["people", "notion", "wiki_skills"],
                                      "default": "people",
                                      "description": "Defaults to people."}},
                       "required": ["query"]},
    },
]


# Verify-or-be-honest harness wrapped around EVERY delegated instruction. The
# background agent has file/bash tools, so it can observe the real end-state — this
# forces it to, instead of declaring success off a proxy (a script's "done" echo, a
# tool's own success message). The closing VERIFIED/UNVERIFIED/FAILED tag is what the
# voice agent relays out loud, so an honest "couldn't confirm" reaches the user.
VERIFY_HARNESS = """Work to a VERIFIED end-state — not an "I ran the command" proxy.

1. GOAL AS OBSERVABLE STATE: before acting, restate the task as a concrete, checkable end-state — what should be TRUE and directly observable when it's done (a file's contents, a command's output, a value on screen, a process running).
2. ACT: do the task.
3. VERIFY INDEPENDENTLY: confirm that end-state by OBSERVING it directly — read the file back, re-run the query, check the actual result. Never trust a tool's own "done"/success message or a script's echo: that is a proxy, not proof.
4. ITERATE: if verification fails, diagnose and try a DIFFERENT approach. Repeat act→verify until the end-state actually holds, or you've genuinely exhausted reasonable approaches.
5. REPORT HONESTLY — end your final message with exactly one tag line:
   VERIFIED: <what's true now, and how you observed it>
   UNVERIFIED: <what you did, what you could NOT confirm, and why>
   FAILED: <what blocked it, what you tried>
Never claim success you didn't independently observe. An honest "couldn't confirm" beats a false "done"."""


def _verify_wrap(instruction: str) -> str:
    """Prepend the verify-or-be-honest harness to a delegated instruction."""
    return f"{VERIFY_HARNESS}\n\n--- TASK ---\n{instruction}"


def _completion_signal(out: str, done: str) -> str:
    """Instruction appended to WATCHED tasks. An interactive agent never exits, so the
    shell can't drop the .done sentinel — instead the agent self-reports: its last action
    writes the final report (incl. the VERIFIED/… tag) to <out> and touches <done>, which
    the menubar poller already watches to auto-wake and speak. No duplicate run, no stream
    parsing — same wake path as headless."""
    return ("\n\n--- SIGNAL COMPLETION (REQUIRED) ---\n"
            "You are running in a terminal the user is watching live, and your process will "
            "NOT exit on its own — so the voice agent only learns you're done if you tell it. "
            "As your VERY LAST action, after you have finished AND verified, use your shell "
            "tool exactly once to write your final report (a short summary plus the "
            "VERIFIED/UNVERIFIED/FAILED tag line) to the result file and then create the done "
            "marker:\n"
            f"  cat > {shlex.quote(out)} <<'REPORT'\n"
            "  <your final summary and the VERIFIED/UNVERIFIED/FAILED tag>\n"
            "REPORT\n"
            f"  touch {shlex.quote(done)}\n"
            "Without this, the user never hears your result.")


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
    instruction = _verify_wrap(instruction)
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
        watch = bool(live.get("show_task_terminals"))
        # Watched tasks self-report completion (the agent writes <out> + touches <done>);
        # headless tasks have the shell do it via the runner below.
        prompt_text = instruction + _completion_signal(out, done) if watch else instruction
        with open(pf, "w", encoding="utf-8") as f:
            f.write(prompt_text)
        # $(cat prompt) avoids any shell-injection from the instruction text itself.
        # </dev/null is essential: detached under the live shell, the agent would
        # otherwise inherit an open stdin that never EOFs and block forever (0% CPU,
        # no output, no .done — so the auto-wake never fires).
        runner = (f'{agent_cmd} "$(cat {shlex.quote(pf)})" </dev/null > {shlex.quote(out)} 2>&1; '
                  f'touch {shlex.quote(done)}')
        if watch:
            # Open the agent INTERACTIVELY in its own Terminal (real TTY → the full live
            # agent UI: tool calls, streaming, progress). The instruction is passed as the
            # initial message, exactly as if the user typed it. NO `-p` and NO pipe — a pipe
            # strips the TTY and pi falls back to writing only its final answer. The agent
            # self-reports completion (see _completion_signal) so the watcher still auto-wakes
            # and speaks the result — watching AND a spoken result, from one run.
            interactive_cmd = agent_cmd.replace("pi -p", "pi").replace("claude -p", "claude")
            # A new Terminal window opens in $HOME, so claude/pi would prompt "trust this
            # folder?" every time. cd into the workspace (already a trusted folder) first so
            # the agent starts where the project lives and the trust dialog never fires.
            ws = os.path.expanduser(live.get("workspace") or "~")
            term = f'cd {shlex.quote(ws)} && {interactive_cmd} "$(cat {shlex.quote(pf)})"'
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
        # ponytail: encode bytes ourselves — text=True uses the locale encoding, which is
        # ASCII in the py2app bundle, so em-dashes/emoji crashed pbcopy with a UnicodeError.
        subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
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


if __name__ == "__main__":
    # Self-check: every built delegate command must carry the verify harness (so the
    # background agent verifies the real end-state), and delegation-off must return None.
    import tempfile
    cfg = {"live": {"delegate": "pi", "show_task_terminals": False}}
    orig_tasks = config.TASKS_DIR
    config.TASKS_DIR = tempfile.mkdtemp()
    try:
        cmd, out = _build_delegate_cmd("organize my desktop icons", cfg)
        pf = os.path.join(config.TASKS_DIR,
                          [f for f in os.listdir(config.TASKS_DIR) if f.endswith(".prompt")][0])
        with open(pf, encoding="utf-8") as f:
            written = f.read()
        assert written.startswith(VERIFY_HARNESS), "harness missing from delegated prompt"
        assert "--- TASK ---" in written and "desktop icons" in written
        assert _build_delegate_cmd("anything", {"live": {"delegate": "off"}}) is None
        assert _build_delegate_cmd("", cfg) is None
        # Watch mode: the Terminal must cd into the trusted workspace (no trust prompt),
        # and the prompt must carry the self-report completion signal so the watcher still
        # auto-wakes and speaks — referencing this task's own .out and .done paths.
        wcmd, wout = _build_delegate_cmd("x", {"live": {"delegate": "claude",
                                                        "show_task_terminals": True,
                                                        "workspace": "~/Thrivbe-AI"}})
        assert "cd " in wcmd and "Thrivbe-AI" in wcmd, "watch-mode cmd must cd into workspace"
        wdone = wout[:-4] + ".done"
        wprompt = next(p for p in (os.path.join(config.TASKS_DIR, f)
                                   for f in os.listdir(config.TASKS_DIR) if f.endswith(".prompt"))
                       if "SIGNAL COMPLETION" in open(p, encoding="utf-8").read())
        wtext = open(wprompt, encoding="utf-8").read()
        assert wout in wtext and wdone in wtext, "completion signal must name this task's out+done"
        # Headless prompts must NOT carry it (the shell drops .done for them).
        hcmd, hout = _build_delegate_cmd("y", {"live": {"delegate": "pi"}})
        hprompt = hout[:-4] + ".prompt"
        assert "SIGNAL COMPLETION" not in open(hprompt, encoding="utf-8").read(), \
            "headless prompt must not carry the completion signal"
        print("tools self-check OK — verify harness + watch self-report wired")
    finally:
        config.TASKS_DIR = orig_tasks
